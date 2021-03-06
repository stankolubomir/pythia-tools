# environment variables
from dotenv import load_dotenv
load_dotenv()

# logging
import logging.config
from utils.config import LOGGING
logger = logging.getLogger(__name__)
logging.config.dictConfig(LOGGING)
logger.setLevel(logging.INFO)

import argparse
import json
import os
import re
import pandas as pd
import numpy as np
import sqlalchemy
import joblib

from datetime import datetime, timedelta
from typing import List, Dict, Tuple
from dateutil.parser import parse
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import precision_recall_fscore_support
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import MinMaxScaler, LabelEncoder
from sqlalchemy import func
from google.oauth2 import service_account

from utils.config import LABELS, FeatureColumns, CURRENT_MODEL_VERSION, AGGREGATION_FUNCTIONS_w_ALIASES, \
    MIN_TRAINING_DAYS
from utils.enums import SplitType, NormalizedFeatureHandling, DataRetrievalMode
from utils.enums import ArtifactRetentionMode, ArtifactRetentionCollection, ModelArtifacts
from utils.db_utils import create_connection
from utils.bigquery import get_feature_frame_via_sqlalchemy
from utils.mysql import get_payment_history_features, get_global_context
from utils.data_transformations import unique_list, row_wise_normalization


class ConversionPredictionModel(object):
    def __init__(
            self,
            min_date: datetime = datetime.utcnow() - timedelta(days=31),
            max_date: datetime = datetime.utcnow() - timedelta(days=1),
            moving_window_length: int = 7,
            normalization_handling: NormalizedFeatureHandling = NormalizedFeatureHandling.REPLACE_WITH,
            outcome_labels: List[str] = tuple(LABELS.keys()),
            overwrite_files: bool = True,
            training_split_parameters=None,
            undersampling_factor=300,
            # This applies to all model artifacts that are not part of the flow output
            artifact_retention_mode: ArtifactRetentionMode = ArtifactRetentionMode.DUMP,
            # By default everything gets stored (since we expect most runs to still be in experimental model
            artifacts_to_retain: ArtifactRetentionCollection = ArtifactRetentionCollection.MODEL_TUNING,
            feature_aggregation_functions: Dict[str, sqlalchemy.func] = {'avg': func.avg},
            dry_run: bool = False,
            path_to_model_files: str = None
    ):
        self.min_date = min_date
        self.max_date = max_date
        self.moving_window = moving_window_length
        self.overwrite_files = overwrite_files
        self.user_profiles = None
        self.normalization_handling = normalization_handling
        self.feature_aggregation_functions = feature_aggregation_functions
        self.feature_columns = FeatureColumns(self.feature_aggregation_functions.keys())
        self.category_list_dict = {}
        self.le = LabelEncoder()
        self.outcome_labels = outcome_labels
        self.le.fit(outcome_labels)
        self.X_train = pd.DataFrame()
        self.X_test = pd.DataFrame()
        self.Y_train = pd.Series()
        self.Y_test = pd.Series()
        self.scaler = MinMaxScaler()
        self.model_date = None
        self.training_split_parameters = training_split_parameters
        self.undersampling_factor = undersampling_factor
        self.model = None
        self.outcome_frame = None
        self.scoring_date = datetime.utcnow()
        self.prediction_data = pd.DataFrame()
        self.predictions = pd.DataFrame()
        self.artifact_retention_mode = artifact_retention_mode
        self.artifacts_to_retain = [artifact.value for artifact in artifacts_to_retain.value]
        self.path_to_model_files = os.getenv('PATH_TO_MODEL_FILES', path_to_model_files)
        if not os.path.exists(self.path_to_model_files):
            os.mkdir(self.path_to_model_files)
        self.path_to_commerce_csvs = os.getenv('PATH_TO_COMMERCE_CSV_FILES')
        self.negative_outcome_frame = pd.DataFrame()
        self.browser_day_combinations_original_set = pd.DataFrame()
        self.variable_importances = pd.Series()
        self.dry_run = dry_run
        self.prediction_job_log = None

    def artifact_handler(self, artifact: ModelArtifacts):
        '''
        :param artifact:
        :return:
        Requires::w
            - artifact_retention_mode
            - path_to_model_files
            - if model artifact
                - model_date
                - model
            - else (case for all data frame artifacts):
                - min_date
                - max_date
        A function that handles model artifacts that we don't need as an output currently by either
        storing and dumping or by straight up dumping them
        '''
        if self.artifact_retention_mode == ArtifactRetentionMode.DUMP:
            if artifact == ModelArtifacts.MODEL:
                joblib.dump(
                    self.model,
                    f'{self.path_to_model_files}model_{self.model_date}.pkl'
                )
            else:
                getattr(self, artifact.value, pd.DataFrame())\
                    .to_csv(f'{self.path_to_model_files}artifact_{artifact.value}_{self.min_date}_{self.max_date}.csv')
                logger.info(f'  * {artifact.value} artifact dumped to {self.path_to_model_files}')
        delattr(self, artifact.value)
        logger.info(f'  * {artifact.value} artifact dropped')

    def get_user_profiles_by_date(
            self,
            data_retrieval_mode: DataRetrievalMode = DataRetrievalMode.MODEL_TRAIN_DATA
    ):
        '''
        Requires:
            - min_date
            - max_date
            - moving_window
            - undersampling factor
        Retrieves rolling window user profiles from the db
        using row-wise normalized features
        '''
        self.user_profiles = pd.DataFrame()
        self.min_date = self.min_date.replace(hour=0, minute=0, second=0, microsecond=0)
        self.max_date = self.max_date.replace(hour=0, minute=0, second=0, microsecond=0)

        self.user_profiles = get_feature_frame_via_sqlalchemy(
            self.min_date,
            self.max_date,
            self.moving_window,
            self.feature_aggregation_functions,
            data_retrieval_mode
        )

        logger.info(f'  * Query finished, processing retrieved data')

        for column in [column for column in self.feature_columns.return_feature_list()
                       if column not in self.user_profiles.columns
                       and column not in [
                              'clv', 'days_since_last_subscription', 'article_pageviews_count',
                              'sum_paid', 'avg_price'] +
                       [  # Iterate over all aggregation function types
                              f'pageviews_{aggregation_function_alias}'
                              for aggregation_function_alias in self.feature_aggregation_functions.keys()
                       ]
                       ]:
            self.user_profiles[column] = 0.0
        logger.info(f'  * Retrieved initial user profiles frame from DB')

        try:
            self.get_contextual_features_from_mysql()
            self.feature_columns.add_global_context_features()
            logger.info('Successfully added global context features from mysql')
        except Exception as e:
            logger.info(
                f'''Failed adding global context features from mysql with exception:
                {e};
                proceeding with remaining features''')
            # To make sure these columns are filled in case of failure to retrieve
            # We want them appearing in the same order to avoid having to reorder columns
            for column in ['article_pageviews_count', 'sum_paid', 'pageviews_count', 'avg_price']:
                self.user_profiles[column] = 0.0

        # try:
        #     self.get_payment_window_features_from_csvs()
        #     self.feature_columns.add_commerce_csv_features()
        #     logger.info('Successfully added commerce flow features from csvs')
        # except Exception as e:
        #     logger.info(
        #         f'''Failed adding commerce flow features from csvs with exception:
        #         {e};
        #         proceeding with remaining features''')
        #     # To make sure these columns are filled in case of failure to retrieve
        #     for column in ['checkout', 'payment', 'purchase']:
        #         self.user_profiles[column] = 0.0

        self.user_profiles['date'] = pd.to_datetime(self.user_profiles['date']).dt.date

        try:
            self.get_user_history_features_from_mysql()
            self.feature_columns.add_payment_history_features()
            logger.info('Successfully added user payment history features from mysql')
        except Exception as e:
            logger.info(
                f'''Failed adding payment history features from mysql with exception:
                {e};
                proceeding with remaining features''')
            for column in ['clv', 'days_since_last_subscription']:
                self.user_profiles[column] = 0.0

        self.user_profiles[self.feature_columns.numeric_columns_with_window_variants].fillna(0.0, inplace=True)
        self.user_profiles['user_ids'] = self.user_profiles['user_ids'].apply(unique_list)
        logger.info('  * Initial data validation success')

    def get_payment_window_features_from_csvs(self):
        '''
        Requires:
            - min_date
            - max_date
        Loads in csvs (location needs to be in .env), counts the number of events per browser_id and joins these counts
        to the main feature frame
        '''
        commerce = pd.DataFrame()
        # TODO: remove commented out code in case testing the line below is a success
        dates = [date.date()
                 for date in pd.date_range(self.user_profiles['date'].min() - timedelta(days=7), self.max_date.date())]
        dates = [re.sub('-', '', str(date)) for date in dates]
        for date in dates:
            commerce_daily = pd.read_csv(f'{self.path_to_commerce_csvs}commerce_{date}.csv.gz')
            commerce = commerce.append(
                    commerce_daily,
                    sort=False
                    )

        commerce = commerce[commerce['browser_id'].isin(self.user_profiles['browser_id'].unique())]
        commerce['date'] = pd.to_datetime(commerce['time']).dt.date

        commerce['dummy_column'] = 1.0
        commerce_features = commerce[['date', 'step', 'dummy_column', 'browser_id']].groupby(
            ['date', 'step', 'browser_id']).sum().reset_index()
        commerce_features.head()

        commerce_pivoted = pd.DataFrame()

        # We are using all the commerce steps based on the assumption that all of our postgres data is filtered for only
        # days before conversion, we will do a left join on the postgres data assuming this removes look-ahead
        for step in commerce['step'].unique():
            if commerce_pivoted.empty:
                commerce_pivoted = commerce_features[commerce_features['step'] == step].rename(
                    columns={'dummy_column': step})
            else:
                commerce_pivoted = commerce_pivoted.merge(
                    right=commerce_features[commerce_features['step'] == step].rename(columns={'dummy_column': step}),
                    on=['browser_id', 'date'],
                    how='outer'
                )
            commerce_pivoted.drop('step', axis=1, inplace=True)

        commerce_pivoted.index = commerce_pivoted['date']
        commerce_pivoted.drop('date', axis=1, inplace=True)
        commerce_pivoted.index = pd.to_datetime(commerce_pivoted.index)

        commerce_pivoted.fillna(0.0, inplace=True)
        dates = self.user_profiles['date'].unique()
        dates = [date.date() for date in pd.date_range(dates.min() - timedelta(days=7), dates.max())]
        rolling_commerce_pivotted = (commerce_pivoted.groupby(['browser_id'])
                                     .apply(lambda x: x.reindex(dates)  # fill in the missing dates for each group)
                                            .fillna(0.0)  # fill each missing group with 0
                                            .rolling(7, min_periods=1)
                                            .sum()))  # do a rolling sum

        rolling_commerce_pivotted.reset_index(inplace=True)

        rolling_commerce_pivotted = rolling_commerce_pivotted[
            (rolling_commerce_pivotted['date'] >= self.min_date.date()) &
            (rolling_commerce_pivotted['date'] <= self.max_date.date())
            ]
        rolling_commerce_pivotted['date'] = pd.to_datetime(rolling_commerce_pivotted['date']).dt.date

        self.user_profiles = self.user_profiles.merge(
            right=rolling_commerce_pivotted,
            on=['browser_id', 'date'],
            how='left',
            copy=False
        )
        # TODO: Come up with a better handling for these features for past positives (currently no handling)

    def get_user_history_features_from_mysql(self):
        '''
        Requires:
            - max_date
        Retrieves clv and days since last subscription from the predplatne database data is then written to the main
        feature frame iterating over rows of payment history features, since each feature frame row might contain
        multiple user ids. Currently there is no logic for when there are multiple user ids, we simply use data
        from the last relevant payment history row.
        '''
        payment_history_features = get_payment_history_features(self.max_date)

        # We'll only be matching the first user_id in the list for speed reasons
        self.user_profiles['first_user_id'] = self.user_profiles['user_ids'].apply(
            lambda x: x[0]
        ).str.extract('([0-9]+)')
        # TODO: Come up with a better handling for these features for past positives (currently no handling)
        # for index, row in payment_history_features.iterrows():
        #     if index % int(len(payment_history_features) / 10) == 0:
        #         print(f'{index / len(payment_history_features)} % done')
        #     possible_matches = self.user_profiles[
        #         self.user_profiles['user_ids'] != pd.Series([[''] for i in range(len(self.user_profiles))])
        #     ]
        #     matching_index = possible_matches[
        #         # user_id contained in the list of user_ids for a browser
        #         (possible_matches['user_ids'].astype(str).fillna('').str.contains(str(row['user_id']))) &
        #         # user is not a past positive, since the payment history would be a look ahead
        #         (possible_matches['date'] >= row['last_subscription_end'].date())
        #         ].index
        #     self.user_profiles.loc[matching_index, 'days_since_last_subscription'] = row['days_since_last_subscription']
        #     self.user_profiles.loc[matching_index, 'clv'] = row['clv']
        
        payment_history_features['user_id'] = payment_history_features['user_id'].astype(int).astype(str)
        self.user_profiles = self.user_profiles.merge(
            right=payment_history_features,
            left_on='first_user_id',
            right_on='user_id',
            how='left'
        )

        self.user_profiles['clv'] = self.user_profiles['clv'].astype(float)
        self.user_profiles['clv'].fillna(0.0, inplace=True)
        # In case the user had additional subscriptions that have an end date after the date of the user profile,
        # we treat it as a missing value.
        # TODO: Add better treatment for look-ahead removal
        self.user_profiles['last_subscription_end'] = pd.to_datetime(self.user_profiles['last_subscription_end']).dt.date 
        self.user_profiles.loc[
            self.user_profiles['last_subscription_end'] >= self.user_profiles['date'],
            'clv'
        ] = 0.0
        # The 1000 days is an arbitrary choice here
        self.user_profiles['days_since_last_subscription'].fillna(1000.0, inplace=True)
        self.user_profiles.loc[
            self.user_profiles['last_subscription_end'] >= self.user_profiles['date'],
            'days_since_last_subscription'
        ] = 1000.0

        self.user_profiles.drop(['last_subscription_end', 'first_user_id'], axis=1, inplace=True)

        return payment_history_features

    def get_contextual_features_from_mysql(self):
        '''
        Requires:
            - user_profiles
        Retrieves & joins daily rolling article pageviews, sum paid, payment count and average price
        '''
        # We extract these, since we also want global context for the past positives data
        context = get_global_context(
            self.user_profiles['date'].min(),
            self.user_profiles['date'].max()
        )
        
        context.index = context['date']
        context.drop('date', axis=1, inplace=True)
        context.index = pd.to_datetime(context.index)
        rolling_context = (context.groupby('date')
                           .fillna(0)  # fill each missing group with 0
                           .rolling(7, min_periods=1)
                           .sum())  # do a rolling sum
        rolling_context.reset_index(inplace=True)
        rolling_context['avg_price'] = rolling_context['sum_paid'] / rolling_context['payment_count']
        rolling_context['date'] = pd.to_datetime(rolling_context['date']).dt.date
        # create str variant of the date columns since we can't join on date in pandas
        rolling_context['date_str'] = rolling_context['date'].astype(str)
        self.user_profiles['date_str'] = self.user_profiles['date'].astype(str)

        self.user_profiles = self.user_profiles.merge(
            right=rolling_context,
            on='date_str',
            how='left',
            copy=False
        )

        self.user_profiles.drop(['date_str', 'date_y'], axis=1, inplace=True)
        self.user_profiles.rename(columns={'date_x': 'date'}, inplace=True)

    def introduce_row_wise_normalized_features(self):
        '''
        Requires:
            - user_profiles
            - normalization_handling
            - feature_columns
        Adds normalized profile based features (normalized always within group, either replacing the original profile based
        features in group, or adding them onto the original dataframe
        '''
        self.add_missing_json_columns()
        column_sets = list(self.feature_columns.profile_numeric_columns_from_json_fields.values()) + \
            [time_column_variant for time_column_variant in self.feature_columns.time_based_columns.values()]
        for column_set in column_sets:
            self.user_profiles[column_set] = self.user_profiles[column_set].astype(float)
            normalized_data = pd.DataFrame(row_wise_normalization(np.array(self.user_profiles[column_set])))
            normalized_data.fillna(0.0, inplace=True)
            normalized_data.columns = column_set

            if self.normalization_handling is NormalizedFeatureHandling.REPLACE_WITH:
                self.user_profiles.drop(column_set, axis=1, inplace=True)
            elif self.normalization_handling is NormalizedFeatureHandling.ADD:
                self.feature_columns.add_normalized_profile_features_version(
                    list(self.feature_aggregation_functions.keys())
                )
            else:
                raise ValueError('Unknown normalization handling parameter')

            self.user_profiles = pd.concat(
                [
                    self.user_profiles,
                    normalized_data
                ],
                axis=1
            )
        logger.info('  * Feature normalization success')

    def add_missing_json_columns(self):
        '''
        Requires:
            - user_profiles
            - feature_columns
        In case some of the columns created from json columns are not available i.e. a section hasn't occured
        in the past few days, we fill it with 0.0 to avoid mismatch between datasets. The only way to add / remove
        a section should be via the config file in utils
        '''
        potential_columns = [
            column for column_list_set in
            self.feature_columns.profile_numeric_columns_from_json_fields.values()
            for column_list in column_list_set
            for column in column_list
        ]

        for missing_json_column in set(potential_columns) - set(self.user_profiles.columns):
            self.user_profiles[missing_json_column] = 0.0

    def create_feature_frame(
            self,
            offset_limit_tuple: Tuple = None,
            data_retrieval_mode: DataRetrievalMode = DataRetrievalMode.MODEL_TRAIN_DATA
    ):
        '''
        Requires:
            - min_date
            - max_date
            - moving_window
            - normalization_handling
            - feature_columns
        Feature frame applies basic sanitization (Unknown / bool columns transformation) and keeps only users
        that were active a day ago
        '''
        logger.info(f'  * Loading user profiles')
        self.get_user_profiles_by_date(data_retrieval_mode)
        logger.info(f'  * Processing user profiles')
        test_outcome = self.user_profiles['outcome'].fillna(
            self.user_profiles.groupby('browser_id')['outcome'].fillna(method='bfill')
        )
        test_outcome = test_outcome.fillna(
            self.user_profiles.groupby('browser_id')['outcome'].fillna(method='ffill')
        )
        self.user_profiles['outcome'] = test_outcome
        self.encode_uncommon_categories()
        self.transform_bool_columns_to_int()
        logger.info('  * Filtering user profiles')
        self.user_profiles = self.user_profiles[self.user_profiles['days_active_count'] >= 1].reset_index(drop=True)

        if self.normalization_handling is not NormalizedFeatureHandling.IGNORE:
            logger.info(f'  * Normalizing user profiles')
            self.introduce_row_wise_normalized_features()

    def transform_bool_columns_to_int(self):
        '''
        Requires:
            - user_profiles
        Transform True / False columns into 0/1 columns
        '''
        for column in [column for column in self.feature_columns.bool_columns]:
            self.user_profiles[column] = self.user_profiles[column].apply(lambda value: True if value == 't' else False).astype(int)

    def encode_uncommon_categories(self):
        '''
        Requires:
            - user_profiles
        categorie for columns such as browser with less than 5 % frequency get lumped into 'Other'
        '''
        self.user_profiles.loc[self.user_profiles['device'] == 0, 'device'] = 'Desktop'
        for column in self.feature_columns.categorical_columns:
            column_values_to_recode = list(
                self.user_profiles[column
                ].value_counts(normalize=True)[self.user_profiles[column].value_counts(normalize=True) < 0.05].index)
            self.user_profiles.loc[self.user_profiles[column].isin(column_values_to_recode), column] = 'Other'

    def generate_category_list_dict(self) -> Dict:
        '''
        Requires:
            - user_profiles
        Create lists of individual category variables for consistent encoding between train set, test set and
        prediciton set
        '''
        for column in self.feature_columns.categorical_columns:
            self.category_list_dict[column] = list(self.user_profiles[column].unique()) + ['Unknown']

    def encode_unknown_categorie(self, data: pd.Series):
        '''
        In train and predictiom set, we mighr encounter categorie that weren't present in the test set, in order
        to allow for the prediction algorithm to handle these, we create an Unknown category
        :param data:
        :return:
        '''
        data[~(data.isin(self.category_list_dict[data.name]))] = 'Unknown'

        return data

    def generate_dummy_columns_from_categorical_column(self, data: pd.Series) -> pd.DataFrame:
        '''
        Requires:
            - category_lists_dict
        Generate 0/1 columns from categorical columns handling the logic for Others and Unknown categorie. Always drops
        the Unknown columns (since we only need k-1 columns for k categorie)
        :param data: A column to encode
        :return:
        '''
        data = self.encode_unknown_categorie(data)
        dummies = pd.get_dummies(pd.Categorical(data, categories=self.category_list_dict[data.name]))
        dummies.columns = [data.name + '_' + dummy_column for dummy_column in dummies.columns]
        dummies.drop(columns=data.name + '_Unknown', axis=1, inplace=True)
        dummies.index = data.index

        return dummies

    def replace_dummy_columns_with_dummies(self, data: pd.DataFrame) -> pd.DataFrame:
        '''
        Requires:
            - category_lists_dict
        Generates new dummy columns and drops the original categorical ones
        :param data:
        :param category_lists_dict:
        :return:
        '''
        for column in self.feature_columns.categorical_columns:
            data = pd.concat(
                [
                    data,
                    self.generate_dummy_columns_from_categorical_column(
                        data[column])
                ],
                axis=1)
        data.drop(columns=self.feature_columns.categorical_columns, axis=1, inplace=True)

        return data

    def create_train_test_transformations(self):
        '''
        Requires:
            - model_date
            - training_split_parameters
            - user_profiles
            - feature_columns
            - scaler
            - artifacts_to_retain
            - artifact_retention_mode
        Splits train / test applying dummification and scaling to their variables
        '''
        self.model_date = self.user_profiles['date'].max() + timedelta(days=1)
        split = SplitType(self.training_split_parameters['split'])
        split_ratio = self.training_split_parameters['split_ratio']
        if split is SplitType.RANDOM:
            train, test = train_test_split(self.user_profiles, test_size=(1 - split_ratio), random_state=42)
            train_indices = train.index
            test_indices = test.index
            del (train, test)
        else:
            dates = pd.to_datetime(
                pd.Series([date.date() for date in pd.date_range(
                    self.min_date,
                    self.max_date)]
                    )
                )

            train_date = dates[0:int(round(len(dates) * split_ratio, 0))].max()
            train_indices = self.user_profiles[self.user_profiles['date'] <= train_date.date()].index
            test_indices = self.user_profiles[self.user_profiles['date'] > train_date.date()].index

        self.X_train = self.user_profiles.iloc[train_indices].drop(columns=['outcome', 'user_ids'])
        self.X_test = self.user_profiles.iloc[test_indices].drop(columns=['outcome', 'user_ids'])
        self.generate_category_list_dict()

        with open(
                f'{self.path_to_model_files}category_lists_{self.model_date}.json', 'w') as outfile:
            json.dump(self.category_list_dict, outfile)

        self.X_train = self.replace_dummy_columns_with_dummies(self.X_train)
        self.X_test = self.replace_dummy_columns_with_dummies(self.X_test)

        self.Y_train = self.user_profiles.loc[train_indices, 'outcome'].sort_index()
        self.Y_test = self.user_profiles.loc[test_indices, 'outcome'].sort_index()

        logger.info('  * Dummy variables generation success')

        X_train_numeric = self.user_profiles.loc[
            train_indices,
            self.feature_columns.numeric_columns_with_window_variants
        ].fillna(0)
        X_test_numeric = self.user_profiles.loc[
            test_indices,
            self.feature_columns.numeric_columns_with_window_variants
        ].fillna(0)

        X_train_numeric = pd.DataFrame(self.scaler.fit_transform(X_train_numeric), index=train_indices,
                                       columns=self.feature_columns.numeric_columns_with_window_variants).sort_index()

        X_test_numeric = pd.DataFrame(self.scaler.transform(X_test_numeric), index=test_indices,
                                      columns=self.feature_columns.numeric_columns_with_window_variants).sort_index()

        logger.info('  * Numeric variables handling success')

        self.X_train = pd.concat([X_train_numeric.sort_index(), self.X_train[
            [column for column in self.X_train.columns
             if column not in self.feature_columns.numeric_columns_with_window_variants +
             self.feature_columns.config_columns +
             self.feature_columns.bool_columns]
        ].sort_index()], axis=1)
        self.X_test = pd.concat([X_test_numeric.sort_index(), self.X_test[
            [column for column in self.X_train.columns
             if column not in self.feature_columns.numeric_columns_with_window_variants +
             self.feature_columns.config_columns +
             self.feature_columns.bool_columns]
        ].sort_index()], axis=1)

        joblib.dump(
            self.scaler,
            f'{self.path_to_model_files}scaler_{self.model_date}.pkl'
        )

        # This is used later on for excluding negatives that we already evaluated
        self.browser_day_combinations_original_set = self.user_profiles.loc[
            # We only pick train indices, since we will be iterating over all negatives and it's simpler to do the
            # ones included in the undersampled test set again than to introduce some special handling
            train_indices,
            ['browser_id', 'date', 'outcome']
        ]
        self.browser_day_combinations_original_set['used_in_training'] = True

        if ModelArtifacts.USER_PROFILES.value not in self.artifacts_to_retain:
            self.artifact_handler(ModelArtifacts.USER_PROFILES)

    def delete_existing_model_file_for_same_date(self, filename: str) -> datetime.date:
        '''
        Requires:
            - model_date
            - path_to_model_files
        Deletes model files should they already exist for the given date
        '''
        if filename != 'category_lists':
            suffix = 'pkl'
        else:
            suffix = 'json'

        if self.path_to_model_files in os.listdir(None):
            if f'scaler_{self.model_date}.pkl' in os.listdir(
                    None if self.path_to_model_files == '' else self.path_to_model_files):
                os.remove(f'{self.path_to_model_files}{filename}_{self.model_date}.{suffix}')

    def train_model(
            self,
            model_function,
            model_arguments
    ):
        '''
        Requires:
            - user_profiles
            - undersampling_factor
            - model_arguments
            - artifacts_to_retain
            - artifact_retention_mode
        Trains a new model given a full dataset
        '''
        if 0 not in self.user_profiles['outcome'].unique():
            self.user_profiles['outcome'] = self.le.transform(self.user_profiles['outcome'])

        self.create_train_test_transformations()
        self.X_train.fillna(0.0, inplace=True)
        self.X_test.fillna(0.0, inplace=True)

        logger.info('  * Commencing model training')

        classifier_instance = model_function(**model_arguments)
        self.model = classifier_instance.fit(self.X_train, self.Y_train)

        logger.info('  * Model training complete, generating outcome frame')

        self.outcome_frame = self.create_outcome_frame(
            {
                'train': self.Y_train,
                'test': self.Y_test
            },
            {
                'train': self.model.predict(self.X_train),
                'test': self.model.predict(self.X_test)
            },
            self.outcome_labels,
            self.le
        )
        try:
            self.variable_importances = pd.Series(
                data=self.model.feature_importances_,
                index=self.X_train.columns
            )
        except:
            # This handles parameter tuning, when some model types may not have variable importance
            self.variable_importances = pd.Series()

        logger.info('  * Outcome frame generated')

    @staticmethod
    def create_outcome_frame(
            labels_actual: Dict[str, np.array],
            labels_predicted: Dict[str, np.array],
            outcome_labels,
            label_encoder
    ):
        if len(labels_actual) != len(labels_predicted):
            raise ValueError('Trying to pass differing lengths of actual and predicted data')
        elif labels_actual.keys() != labels_predicted.keys():
            raise ValueError('Unaligned number of outcome sets provided')

        def format_outcome_frame(
                sets_in_outcome: List[str] = ['train', 'test']
        ):
            '''
            Takes a given outcome frame and polishes the row & column names
            :param outcome_frame:
            :param label_range:
            :param label_encoder:
            :param sets_in_outcome
            :return:
            '''
            train_outcome_columns = (
                [str(label) + '_train' for label in outcome_frame.columns[0:len(label_range)]]
                if 'train' in sets_in_outcome
                else []
            )

            test_outcome_columns = (
                [
                    str(label) + '_test' for label in outcome_frame.columns[
                                                      # We either have 6 columns (3 for train and 3 for test) or 3 (test only), therefore we need to adjust indexing
                                                      len(label_range) * (len(sets_in_outcome) - 1):(
                                                                  len(sets_in_outcome) * len(label_range))]
                ]
                if 'test' in sets_in_outcome
                else []
            )

            outcome_frame.columns = train_outcome_columns + test_outcome_columns

            for i in label_range:
                outcome_frame.columns = [re.sub(str(i), label_encoder.inverse_transform([i])[0], column)
                                         for column in outcome_frame.columns]

            outcome_frame.index = ['precision', 'recall', 'f-score', 'support']

        label_range = list(range(0, len(outcome_labels)))
        outcome_frame = pd.DataFrame()
        for i in range(0, len(labels_actual)):
            actual = list(labels_actual.values())[i]
            predicted = list(labels_predicted.values())[i]
            outcome_frame_partial = pd.DataFrame(
                list(
                    precision_recall_fscore_support(
                        actual,
                        predicted,
                        labels=label_range
                    )
                )
            )

            if outcome_frame.empty:
                outcome_frame = outcome_frame_partial
            else:
                outcome_frame = pd.concat(
                    [
                        outcome_frame,
                        outcome_frame_partial
                    ],
                    axis=1
                )

        format_outcome_frame(
            sets_in_outcome=list(labels_actual.keys())
        )

        return outcome_frame

    def remove_rows_from_original_flow(self):
        logger.info('  * Commencing accuracy metrics for negatives calculation')
        self.user_profiles = pd.merge(
            left=self.user_profiles,
            right=self.browser_day_combinations_original_set[['date', 'browser_id', 'used_in_training']],
            on=['date', 'browser_id'],
            how='left'
        )

        self.user_profiles = self.user_profiles[
            self.user_profiles['used_in_training'].isna()
        ].reset_index(drop=True)

        self.user_profiles.drop('used_in_training', axis=1, inplace=True)

    def model_training_pipeline(
            self,
            model_function=RandomForestClassifier,
            model_arguments={'n_estimators': 250}
    ):
        '''
        Requires:
            - undersampling_factor
            - model_arguments
            - artifacts_to_retain
            - artifact_retention_mode
            - min_date
            - max_date
            - moving_window
            - normalization_handling
            - feature_columns
            - model_date
            - path_to_model_files
        Pipeline that outputs a trained model and it's accuracy measures
        '''
        logger.info(f'Executing training pipeline')
        if self.user_profiles is None:

            # Make sure we have enough days for training, this statement is behind the condition since sometimes
            # we might be reusing training data from a previous run
            #if (self.max_date - self.min_date).days < MIN_TRAINING_DAYS:
            #    raise ValueError(f'Date range too small. Please provide at least {MIN_TRAINING_DAYS} days of data')

            self.create_feature_frame(data_retrieval_mode=DataRetrievalMode.MODEL_TRAIN_DATA)

        if self.overwrite_files:
            for model_file in ['category_lists', 'scaler', 'model']:
                self.delete_existing_model_file_for_same_date(model_file)

        self.train_model(
            model_function,
            model_arguments
        )

        logger.info(f'Training ready, dumping to file')

        joblib.dump(
            self.model,
            f'{self.path_to_model_files}model_{self.model_date}.pkl'
        )

        logger.info(f'Saved to {self.path_to_model_files}model_{self.model_date}.pkl')

        self.remove_model_training_artefacts()
        # TODO: This would eventually be replaced with storing variable importances to DB
        self.variable_importances.to_csv(
            f'{self.path_to_model_files}variable_importances_{self.model_date}.csv',
            index=True,
            header=False
        )

    def remove_model_training_artefacts(self):
        for artifact in [
            ModelArtifacts.TRAIN_DATA_FEATURES, ModelArtifacts.TRAIN_DATA_OUTCOME,
            ModelArtifacts.TEST_DATA_FEATURES, ModelArtifacts.TEST_DATA_OUTCOME,
        ]:
            if artifact.value not in self.artifacts_to_retain:
                self.artifact_handler(artifact)

    def load_model_related_constructs(self):
        '''
        Requires:
            - path_to_model_files
            - scoring_date
        Serves for the prediction pipeline in order to load model & additional transformation config objects
        '''

        model_related_file_list = os.listdir(self.path_to_model_files)
        last_model_related_files = {}
        for model_related_file in ['category_lists', 'scaler', 'model', 'variable_importances']:
            last_file_date = {parse(re.sub(f'{model_related_file}_|.json|.pkl|.csv|None', '', filename)):
                              abs(parse(re.sub(f'{model_related_file}_|.json|.pkl|.csv|None', '', filename)).date()
                                  - self.scoring_date.date())
                              for filename in model_related_file_list
                              if re.search(f'{model_related_file}_', filename)}
            last_file_date = [date for date, diff in last_file_date.items() if diff == min(last_file_date.values())][0]
            last_model_related_files[model_related_file] = last_file_date.date()
        if len(set(last_model_related_files.values())) > 1:
            raise ValueError(f'''Unaligned model file dates
                category_list date: {last_model_related_files['category_lists']}
                scaler date: {last_model_related_files['scaler']}
                model date: {last_model_related_files['model']}
                'variable importances': {last_model_related_files['variable_importances']}
                ''')

        if not self.path_to_model_files:
            self.path_to_model_files = ''
        with open(self.path_to_model_files +
                  'category_lists_' + str(last_model_related_files['category_lists']) + '.json', 'r') as outfile:
            self.category_list_dict = json.load(outfile)

        self.scaler = joblib.load(f"{self.path_to_model_files}scaler_{str(last_model_related_files['scaler'])}.pkl")
        self.model = joblib.load(f"{self.path_to_model_files}model_{str(last_model_related_files['model'])}.pkl")
        #TODO: This would eventually be replaced with loading variable importances from DB
        self.variable_importances = pd.read_csv(
                f"{self.path_to_model_files}variable_importances_{str(last_model_related_files['variable_importances'])}.csv",
                squeeze=True,
                index_col=0,
                header=None
        )

        logger.info('  * Model constructs loaded')

    def batch_predict(self, data):
        '''
        Requires:
            - min_date
            - max_date
            - moving_window
            - normalization_handling
            - feature_columns
            - model_date
            - path_to_model_files
            - scoring_date
            - label_encoder
        Outputs predictions for a given time period
        '''
        logger.info('  * Preparing data for prediction')
        
        if self.model is None:
            self.load_model_related_constructs()

        feature_frame_numeric = pd.DataFrame(
            self.scaler.transform(
                data[
                    self.feature_columns.numeric_columns_with_window_variants]
            ),
            index=data.index,
            columns=self.feature_columns.numeric_columns_with_window_variants).sort_index()

        self.prediction_data = pd.concat([
            feature_frame_numeric, data[
                [column for column in data.columns
                 if column not in self.feature_columns.numeric_columns_with_window_variants +
                 self.feature_columns.config_columns +
                 self.feature_columns.bool_columns]].sort_index()], axis=1)

        self.prediction_data = self.replace_dummy_columns_with_dummies(self.prediction_data)

        self.align_prediction_frame_with_train_columns()

        logger.info('  * Prediction data ready')
        self.prediction_data.fillna(0.0, inplace=True)
        if not self.X_train.empty:
            for column in [column for column in self.X_train.columns if column not in self.prediction_data.columns]:
                self.prediction_data[column] = 0.0
            self.prediction_data = self.prediction_data[list(self.X_train.columns)]
        predictions = pd.DataFrame(self.model.predict_proba(self.prediction_data))
        logger.info('  * Prediction generation success, handling artifacts')

        label_range = range(len(LABELS))

        for i in label_range:
            predictions.columns = [re.sub(str(i), self.le.inverse_transform([i])[0] + '_probability', str(column))
                                   for column in predictions.columns]
        # We are adding outcome only for the sake of the batch test approach, we'll be dropping it in the actual
        # prediction pipeline
        self.predictions = pd.concat(
            [self.user_profiles[['date', 'browser_id', 'user_ids', 'outcome']],
             predictions],
            axis=1
        )
        self.predictions['predicted_outcome'] = self.le.inverse_transform(self.model.predict(self.prediction_data))

    def align_prediction_frame_with_train_columns(self):
        # Sometimes the columns used to train a model don't align with columns im prediction set
        # 1. Drop columns that are in new data, but weren't used in training
        self.prediction_data.drop(
            [column for column in self.prediction_data.columns if column not in self.variable_importances.index],
            axis=1,
            inplace=True
        )

        # 2. Add 0 columns that were in train, but aren't in new data
        for column in [column for column in self.variable_importances.index
                       if column not in self.prediction_data.columns]:
            self.prediction_data[column] = 0.0

        # 3. Make sure the columns have the same order as original data, since sklearn ignores column names
        self.prediction_data = self.prediction_data[list(self.variable_importances.index)]

    def generate_and_upload_prediction(self):
        '''
        Requires:
            - min_date
            - max_date
            - moving_window
            - normalization_handling
            - feature_columns
            - model_date
            - path_to_model_files
            - scoring_date
            - label_encoder
        Generates outcome prediction for conversion and uploads them to the DB
        '''
        logger.info(f'Executing prediction generation')
        self.create_feature_frame(data_retrieval_mode=DataRetrievalMode.PREDICT_DATA)
        self.artifact_retention_mode = ArtifactRetentionMode.DROP

        logger.setLevel(logging.INFO)

        self.batch_predict(self.user_profiles)
        logging.info('  * Generatincg predictions')

        self.predictions['model_version'] = CURRENT_MODEL_VERSION
        self.predictions['created_at'] = datetime.utcnow()
        self.predictions.loc[
            self.predictions['user_ids'].astype(str) == '[]',
            'user_ids'] = None

        # Dry run tends to be used for testing new models, so we want to be able to calculate accuracy metrics
        if not self.dry_run:
            self.predictions.drop('outcome', axis=1, inplace=True)

            logger.info(f'Storing predicted data')
            database = os.getenv('BIGQUERY_PROJECT_ID')
            _, db_connection = create_connection(
                f'bigquery://{database}',
                engine_kwargs={'credentials_path': '../../gcloud_client_secrets.json'}
            )


            credentials = service_account.Credentials.from_service_account_file(
                '../../gcloud_client_secrets.json',
            )

            self.predictions.to_gbq(
                destination_table='pythia.conversion_predictions_log',
                project_id=database,
                credentials=credentials,
                if_exists='append'
            )

            self.prediction_job_log = self.predictions[
                ['date', 'model_version', 'created_at']].head(1)
            self.prediction_job_log['rows_predicted'] = len(self.predictions)

            self.prediction_job_log.to_gbq(
                destination_table='pythia.prediction_job_log',
                project_id=database,
                credentials=credentials,
                if_exists='append',
            )
        else:
            actual_labels = {'test': self.predictions['outcome']}
            predicted_labels = {'test': self.predictions['predicted_outcome']}
            self.outcome_frame = self.create_outcome_frame(
                actual_labels,
                predicted_labels,
                self.outcome_labels,
                self.le
            )

        logger.info('Predictions are now ready')

        for artifact in [ModelArtifacts.MODEL, ModelArtifacts.PREDICTION_DATA, ModelArtifacts.USER_PROFILES]:
            self.artifact_handler(artifact)


def mkdatetime(datestr: str) -> datetime:
    '''
    Parses out a date from input string
    :param datestr:
    :return:
    '''
    try:
        return parse(datestr)
    except ValueError:
        raise ValueError('Incorrect Date String')


if __name__ == "__main__":
    logger.info(f'CONVERSION PREDICTION')
    parser = argparse.ArgumentParser()
    parser.add_argument('--action',
                        help='Should either be "train" for model training or "predict" for prediction',
                        type=str)
    parser.add_argument('--min-date',
                        help='Min date denoting from when to fetch data',
                        type=mkdatetime,
                        required=True)
    parser.add_argument('--max-date',
                        help='Max date denoting up to when to fetch data',
                        type=mkdatetime,
                        default=datetime.utcnow() - timedelta(days=1),
                        required=False)
    parser.add_argument('--moving-window-length',
                        help='Lenght for rolling sum windows for user profiles',
                        type=int,
                        default=7,
                        required=False)
    parser.add_argument('--training-split-parameters',
                        help='Speficies split_type (random vs time_based) and split_ratio for train/test split',
                        type=json.loads,
                        default={'split': 'time_based', 'split_ratio': 6 / 10},
                        required=False)
    parser.add_argument('--model-arguments',
                        help='Parameters for scikit model training',
                        type=json.loads,
                        default={'n_estimators': 250},
                        required=False)
    parser.add_argument('--overwrite-files',
                        help='Bool implying whether newly trained model should overwrite existing one for the same date',
                        type=bool,
                        default=True,
                        required=False)

    args = parser.parse_args()
    args = vars(args)
    if args['min_date'] > args['max_date']:
        raise ValueError('Max date is sooner than the min date')
    if args['action'] == 'train':
        conversion_prediction = ConversionPredictionModel(
            min_date=args['min_date'],
            max_date=args['max_date'],
            moving_window_length=args['moving_window_length'],
            overwrite_files=args['overwrite_files'],
            training_split_parameters=args['training_split_parameters'],
            undersampling_factor=500,
            artifact_retention_mode=ArtifactRetentionMode.DROP,
            artifacts_to_retain=ArtifactRetentionCollection.MODEL_RETRAINING
            )

        conversion_prediction.model_training_pipeline(
            model_arguments={'n_estimators': 250}
        )

        metrics = ['precision', 'recall', 'f1_score', 'suport']
        print({metrics[i]: conversion_prediction.outcome_frame.to_dict('records')[i] for i in range(0, len(metrics))})
    elif args['action'] == 'predict':
        conversion_prediction = ConversionPredictionModel(
            min_date=args['min_date'],
            max_date=args['max_date'],
            undersampling_factor=1,
            moving_window_length=args['moving_window_length'],
            artifact_retention_mode=ArtifactRetentionMode.DROP,
            artifacts_to_retain=ArtifactRetentionCollection.PREDICTION
        )

        conversion_prediction.generate_and_upload_prediction()
