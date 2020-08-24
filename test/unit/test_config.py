from copy import deepcopy
from contextlib import contextmanager
import json
import os
import shutil
import tempfile
import unittest

from unittest import mock
import yaml

import dbt.config
import dbt.exceptions
from dbt.adapters.factory import load_plugin
from dbt.adapters.postgres import PostgresCredentials
from dbt.adapters.redshift import RedshiftCredentials
from dbt.context.base import generate_base_context
from dbt.contracts.connection import QueryComment, DEFAULT_QUERY_COMMENT
from dbt.contracts.project import PackageConfig, LocalPackage, GitPackage
from dbt.node_types import NodeType
from dbt.semver import VersionSpecifier
from dbt.task.run_operation import RunOperationTask

from .utils import normalize, config_from_parts_or_dicts

INITIAL_ROOT = os.getcwd()


@contextmanager
def temp_cd(path):
    current_path = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(current_path)


def empty_profile_renderer():
    return dbt.config.renderer.ProfileRenderer(generate_base_context({}))


def empty_project_renderer():
    return dbt.config.renderer.DbtProjectYamlRenderer(generate_base_context({}))


model_config = {
    'my_package_name': {
        'enabled': True,
        'adwords': {
            'adwords_ads': {
                'materialized': 'table',
                'enabled': True,
                'schema': 'analytics'
            }
        },
        'snowplow': {
            'snowplow_sessions': {
                'sort': 'timestamp',
                'materialized': 'incremental',
                'dist': 'user_id',
                'unique_key': 'id'
            },
            'base': {
                'snowplow_events': {
                    'sort': ['timestamp', 'userid'],
                    'materialized': 'table',
                    'sort_type': 'interleaved',
                    'dist': 'userid'
                }
            }
        }
    }
}

model_fqns = frozenset((
    ('my_package_name', 'snowplow', 'snowplow_sessions'),
    ('my_package_name', 'snowplow', 'base', 'snowplow_events'),
    ('my_package_name', 'adwords', 'adwords_ads'),
))


class Args:
    def __init__(self, profiles_dir=None, threads=None, profile=None,
                 cli_vars=None, version_check=None, project_dir=None):
        self.profile = profile
        if threads is not None:
            self.threads = threads
        if profiles_dir is not None:
            self.profiles_dir = profiles_dir
        if cli_vars is not None:
            self.vars = cli_vars
        if version_check is not None:
            self.version_check = version_check
        if project_dir is not None:
            self.project_dir = project_dir


class BaseConfigTest(unittest.TestCase):
    """Subclass this, and before calling the superclass setUp, set
    self.profiles_dir and self.project_dir.
    """
    def setUp(self):
        self.default_project_data = {
            'version': '0.0.1',
            'name': 'my_test_project',
            'profile': 'default',
        }
        self.default_profile_data = {
            'default': {
                'outputs': {
                    'postgres': {
                        'type': 'postgres',
                        'host': 'postgres-db-hostname',
                        'port': 5555,
                        'user': 'db_user',
                        'pass': 'db_pass',
                        'dbname': 'postgres-db-name',
                        'schema': 'postgres-schema',
                        'threads': 7,
                    },
                    'redshift': {
                        'type': 'redshift',
                        'host': 'redshift-db-hostname',
                        'port': 5555,
                        'user': 'db_user',
                        'pass': 'db_pass',
                        'dbname': 'redshift-db-name',
                        'schema': 'redshift-schema',
                    },
                    'with-vars': {
                        'type': "{{ env_var('env_value_type') }}",
                        'host': "{{ env_var('env_value_host') }}",
                        'port': "{{ env_var('env_value_port') | as_number }}",
                        'user': "{{ env_var('env_value_user') }}",
                        'pass': "{{ env_var('env_value_pass') }}",
                        'dbname': "{{ env_var('env_value_dbname') }}",
                        'schema': "{{ env_var('env_value_schema') }}",
                    },
                    'cli-and-env-vars': {
                        'type': "{{ env_var('env_value_type') }}",
                        'host': "{{ var('cli_value_host') }}",
                        'port': "{{ env_var('env_value_port') | as_number }}",
                        'user': "{{ env_var('env_value_user') }}",
                        'pass': "{{ env_var('env_value_pass') }}",
                        'dbname': "{{ env_var('env_value_dbname') }}",
                        'schema': "{{ env_var('env_value_schema') }}",
                    }
                },
                'target': 'postgres',
            },
            'other': {
                'outputs': {
                    'other-postgres': {
                        'type': 'postgres',
                        'host': 'other-postgres-db-hostname',
                        'port': 4444,
                        'user': 'other_db_user',
                        'pass': 'other_db_pass',
                        'dbname': 'other-postgres-db-name',
                        'schema': 'other-postgres-schema',
                        'threads': 2,
                    }
                },
                'target': 'other-postgres',
            },
            'empty_profile_data': {}
        }
        self.args = Args(profiles_dir=self.profiles_dir, cli_vars='{}',
                         version_check=True, project_dir=self.project_dir)
        self.env_override = {
            'env_value_type': 'postgres',
            'env_value_host': 'env-postgres-host',
            'env_value_port': '6543',
            'env_value_user': 'env-postgres-user',
            'env_value_pass': 'env-postgres-pass',
            'env_value_dbname': 'env-postgres-dbname',
            'env_value_schema': 'env-postgres-schema',
            'env_value_profile': 'default',
        }


class BaseFileTest(BaseConfigTest):
    def setUp(self):
        self.project_dir = normalize(tempfile.mkdtemp())
        self.profiles_dir = normalize(tempfile.mkdtemp())
        super().setUp()

    def tearDown(self):
        try:
            shutil.rmtree(self.project_dir)
        except EnvironmentError:
            pass
        try:
            shutil.rmtree(self.profiles_dir)
        except EnvironmentError:
            pass

    def project_path(self, name):
        return os.path.join(self.project_dir, name)

    def profile_path(self, name):
        return os.path.join(self.profiles_dir, name)

    def write_project(self, project_data=None):
        if project_data is None:
            project_data = self.project_data
        with open(self.project_path('dbt_project.yml'), 'w') as fp:
            yaml.dump(project_data, fp)

    def write_packages(self, package_data):
        with open(self.project_path('packages.yml'), 'w') as fp:
            yaml.dump(package_data, fp)

    def write_profile(self, profile_data=None):
        if profile_data is None:
            profile_data = self.profile_data
        with open(self.profile_path('profiles.yml'), 'w') as fp:
            yaml.dump(profile_data, fp)

    def write_empty_profile(self):
        with open(self.profile_path('profiles.yml'), 'w') as fp:
            yaml.dump('', fp)


class TestProfile(BaseConfigTest):
    def setUp(self):
        self.profiles_dir = '/invalid-path'
        self.project_dir = '/invalid-project-path'
        super().setUp()

    def from_raw_profiles(self):
        renderer = empty_profile_renderer()
        return dbt.config.Profile.from_raw_profiles(
            self.default_profile_data, 'default', renderer
        )

    def test_from_raw_profiles(self):
        profile = self.from_raw_profiles()
        self.assertEqual(profile.profile_name, 'default')
        self.assertEqual(profile.target_name, 'postgres')
        self.assertEqual(profile.threads, 7)
        self.assertTrue(profile.config.send_anonymous_usage_stats)
        self.assertIsNone(profile.config.use_colors)
        self.assertTrue(isinstance(profile.credentials, PostgresCredentials))
        self.assertEqual(profile.credentials.type, 'postgres')
        self.assertEqual(profile.credentials.host, 'postgres-db-hostname')
        self.assertEqual(profile.credentials.port, 5555)
        self.assertEqual(profile.credentials.user, 'db_user')
        self.assertEqual(profile.credentials.password, 'db_pass')
        self.assertEqual(profile.credentials.schema, 'postgres-schema')
        self.assertEqual(profile.credentials.database, 'postgres-db-name')

    def test_config_override(self):
        self.default_profile_data['config'] = {
            'send_anonymous_usage_stats': False,
            'use_colors': False,
        }
        profile = self.from_raw_profiles()
        self.assertEqual(profile.profile_name, 'default')
        self.assertEqual(profile.target_name, 'postgres')
        self.assertFalse(profile.config.send_anonymous_usage_stats)
        self.assertFalse(profile.config.use_colors)

    def test_partial_config_override(self):
        self.default_profile_data['config'] = {
            'send_anonymous_usage_stats': False,
            'printer_width': 60
        }
        profile = self.from_raw_profiles()
        self.assertEqual(profile.profile_name, 'default')
        self.assertEqual(profile.target_name, 'postgres')
        self.assertFalse(profile.config.send_anonymous_usage_stats)
        self.assertIsNone(profile.config.use_colors)
        self.assertEqual(profile.config.printer_width, 60)

    def test_missing_type(self):
        del self.default_profile_data['default']['outputs']['postgres']['type']
        with self.assertRaises(dbt.exceptions.DbtProfileError) as exc:
            self.from_raw_profiles()
        self.assertIn('type', str(exc.exception))
        self.assertIn('postgres', str(exc.exception))
        self.assertIn('default', str(exc.exception))

    def test_bad_type(self):
        self.default_profile_data['default']['outputs']['postgres']['type'] = 'invalid'
        with self.assertRaises(dbt.exceptions.DbtProfileError) as exc:
            self.from_raw_profiles()
        self.assertIn('Credentials', str(exc.exception))
        self.assertIn('postgres', str(exc.exception))
        self.assertIn('default', str(exc.exception))

    def test_invalid_credentials(self):
        del self.default_profile_data['default']['outputs']['postgres']['host']
        with self.assertRaises(dbt.exceptions.DbtProfileError) as exc:
            self.from_raw_profiles()
        self.assertIn('Credentials', str(exc.exception))
        self.assertIn('postgres', str(exc.exception))
        self.assertIn('default', str(exc.exception))

    def test_missing_target(self):
        profile = self.default_profile_data['default']
        del profile['target']
        profile['outputs']['default'] = profile['outputs']['postgres']
        profile = self.from_raw_profiles()
        self.assertEqual(profile.profile_name, 'default')
        self.assertEqual(profile.target_name, 'default')
        self.assertEqual(profile.credentials.type, 'postgres')

    def test_profile_invalid_project(self):
        renderer = empty_profile_renderer()
        with self.assertRaises(dbt.exceptions.DbtProjectError) as exc:
            dbt.config.Profile.from_raw_profiles(
                self.default_profile_data, 'invalid-profile', renderer
            )

        self.assertEqual(exc.exception.result_type, 'invalid_project')
        self.assertIn('Could not find', str(exc.exception))
        self.assertIn('invalid-profile', str(exc.exception))

    def test_profile_invalid_target(self):
        renderer = empty_profile_renderer()
        with self.assertRaises(dbt.exceptions.DbtProfileError) as exc:
            dbt.config.Profile.from_raw_profiles(
                self.default_profile_data, 'default', renderer,
                target_override='nope'
            )

        self.assertIn('nope', str(exc.exception))
        self.assertIn('- postgres', str(exc.exception))
        self.assertIn('- redshift', str(exc.exception))
        self.assertIn('- with-vars', str(exc.exception))

    def test_no_outputs(self):
        renderer = empty_profile_renderer()

        with self.assertRaises(dbt.exceptions.DbtProfileError) as exc:
            dbt.config.Profile.from_raw_profiles(
                {'some-profile': {'target': 'blah'}}, 'some-profile', renderer
            )
        self.assertIn('outputs not specified', str(exc.exception))
        self.assertIn('some-profile', str(exc.exception))

    def test_neq(self):
        profile = self.from_raw_profiles()
        self.assertNotEqual(profile, object())

    def test_eq(self):
        renderer = empty_profile_renderer()
        profile = dbt.config.Profile.from_raw_profiles(
            deepcopy(self.default_profile_data), 'default', renderer
        )

        other = dbt.config.Profile.from_raw_profiles(
            deepcopy(self.default_profile_data), 'default', renderer
        )
        self.assertEqual(profile, other)

    def test_invalid_env_vars(self):
        self.env_override['env_value_port'] = 'hello'
        renderer = empty_profile_renderer()
        with mock.patch.dict(os.environ, self.env_override):
            with self.assertRaises(dbt.exceptions.DbtProfileError) as exc:
                dbt.config.Profile.from_raw_profile_info(
                    self.default_profile_data['default'],
                    'default',
                    renderer,
                    target_override='with-vars'
                )
        self.assertIn("Could not convert value 'hello' into type 'number'", str(exc.exception))


class TestProfileFile(BaseFileTest):
    def setUp(self):
        super().setUp()
        self.write_profile(self.default_profile_data)

    def from_raw_profile_info(self, raw_profile=None, profile_name='default', **kwargs):
        if raw_profile is None:
            raw_profile = self.default_profile_data['default']
        renderer = empty_profile_renderer()
        kw = {
            'raw_profile': raw_profile,
            'profile_name': profile_name,
            'renderer': renderer,
        }
        kw.update(kwargs)
        return dbt.config.Profile.from_raw_profile_info(**kw)

    def from_args(self, project_profile_name='default', **kwargs):
        kw = {
            'args': self.args,
            'project_profile_name': project_profile_name,
            'renderer': empty_profile_renderer()
        }
        kw.update(kwargs)
        return dbt.config.Profile.render_from_args(**kw)

    def test_profile_simple(self):
        profile = self.from_args()
        from_raw = self.from_raw_profile_info()

        self.assertEqual(profile.profile_name, 'default')
        self.assertEqual(profile.target_name, 'postgres')
        self.assertEqual(profile.threads, 7)
        self.assertTrue(profile.config.send_anonymous_usage_stats)
        self.assertIsNone(profile.config.use_colors)
        self.assertTrue(isinstance(profile.credentials, PostgresCredentials))
        self.assertEqual(profile.credentials.type, 'postgres')
        self.assertEqual(profile.credentials.host, 'postgres-db-hostname')
        self.assertEqual(profile.credentials.port, 5555)
        self.assertEqual(profile.credentials.user, 'db_user')
        self.assertEqual(profile.credentials.password, 'db_pass')
        self.assertEqual(profile.credentials.schema, 'postgres-schema')
        self.assertEqual(profile.credentials.database, 'postgres-db-name')
        self.assertEqual(profile, from_raw)

    def test_profile_override(self):
        self.args.profile = 'other'
        self.args.threads = 3
        profile = self.from_args()
        from_raw = self.from_raw_profile_info(
                self.default_profile_data['other'],
                'other',
                threads_override=3,
            )

        self.assertEqual(profile.profile_name, 'other')
        self.assertEqual(profile.target_name, 'other-postgres')
        self.assertEqual(profile.threads, 3)
        self.assertTrue(profile.config.send_anonymous_usage_stats)
        self.assertIsNone(profile.config.use_colors)
        self.assertTrue(isinstance(profile.credentials, PostgresCredentials))
        self.assertEqual(profile.credentials.type, 'postgres')
        self.assertEqual(profile.credentials.host, 'other-postgres-db-hostname')
        self.assertEqual(profile.credentials.port, 4444)
        self.assertEqual(profile.credentials.user, 'other_db_user')
        self.assertEqual(profile.credentials.password, 'other_db_pass')
        self.assertEqual(profile.credentials.schema, 'other-postgres-schema')
        self.assertEqual(profile.credentials.database, 'other-postgres-db-name')
        self.assertEqual(profile, from_raw)

    def test_target_override(self):
        self.args.target = 'redshift'
        profile = self.from_args()
        from_raw = self.from_raw_profile_info(
                target_override='redshift'
            )

        self.assertEqual(profile.profile_name, 'default')
        self.assertEqual(profile.target_name, 'redshift')
        self.assertEqual(profile.threads, 1)
        self.assertTrue(profile.config.send_anonymous_usage_stats)
        self.assertIsNone(profile.config.use_colors)
        self.assertTrue(isinstance(profile.credentials, RedshiftCredentials))
        self.assertEqual(profile.credentials.type, 'redshift')
        self.assertEqual(profile.credentials.host, 'redshift-db-hostname')
        self.assertEqual(profile.credentials.port, 5555)
        self.assertEqual(profile.credentials.user, 'db_user')
        self.assertEqual(profile.credentials.password, 'db_pass')
        self.assertEqual(profile.credentials.schema, 'redshift-schema')
        self.assertEqual(profile.credentials.database, 'redshift-db-name')
        self.assertEqual(profile, from_raw)        

    def test_env_vars(self):
        self.args.target = 'with-vars'
        with mock.patch.dict(os.environ, self.env_override):
            profile = self.from_args()
            from_raw = self.from_raw_profile_info(
                target_override='with-vars'
            )

        self.assertEqual(profile.profile_name, 'default')
        self.assertEqual(profile.target_name, 'with-vars')
        self.assertEqual(profile.threads, 1)
        self.assertTrue(profile.config.send_anonymous_usage_stats)
        self.assertIsNone(profile.config.use_colors)
        self.assertEqual(profile.credentials.type, 'postgres')
        self.assertEqual(profile.credentials.host, 'env-postgres-host')
        self.assertEqual(profile.credentials.port, 6543)
        self.assertEqual(profile.credentials.user, 'env-postgres-user')
        self.assertEqual(profile.credentials.password, 'env-postgres-pass')
        self.assertEqual(profile, from_raw)

    def test_env_vars_env_target(self):
        self.default_profile_data['default']['target'] = "{{ env_var('env_value_target') }}"
        self.write_profile(self.default_profile_data)
        self.env_override['env_value_target'] = 'with-vars'
        with mock.patch.dict(os.environ, self.env_override):
            profile = self.from_args()
            from_raw = self.from_raw_profile_info(
                target_override='with-vars'
            )

        self.assertEqual(profile.profile_name, 'default')
        self.assertEqual(profile.target_name, 'with-vars')
        self.assertEqual(profile.threads, 1)
        self.assertTrue(profile.config.send_anonymous_usage_stats)
        self.assertIsNone(profile.config.use_colors)
        self.assertEqual(profile.credentials.type, 'postgres')
        self.assertEqual(profile.credentials.host, 'env-postgres-host')
        self.assertEqual(profile.credentials.port, 6543)
        self.assertEqual(profile.credentials.user, 'env-postgres-user')
        self.assertEqual(profile.credentials.password, 'env-postgres-pass')
        self.assertEqual(profile, from_raw)

    def test_invalid_env_vars(self):
        self.env_override['env_value_port'] = 'hello'
        self.args.target = 'with-vars'
        with mock.patch.dict(os.environ, self.env_override):
            with self.assertRaises(dbt.exceptions.DbtProfileError) as exc:
                self.from_args()

        self.assertIn("Could not convert value 'hello' into type 'number'", str(exc.exception))

    def test_cli_and_env_vars(self):
        self.args.target = 'cli-and-env-vars'
        self.args.vars = '{"cli_value_host": "cli-postgres-host"}'
        renderer = dbt.config.renderer.ProfileRenderer(generate_base_context({'cli_value_host': 'cli-postgres-host'}))
        with mock.patch.dict(os.environ, self.env_override):
            profile = self.from_args(renderer=renderer)
            from_raw = self.from_raw_profile_info(
                target_override='cli-and-env-vars',
                renderer=renderer,
            )

        self.assertEqual(profile.profile_name, 'default')
        self.assertEqual(profile.target_name, 'cli-and-env-vars')
        self.assertEqual(profile.threads, 1)
        self.assertTrue(profile.config.send_anonymous_usage_stats)
        self.assertIsNone(profile.config.use_colors)
        self.assertEqual(profile.credentials.type, 'postgres')
        self.assertEqual(profile.credentials.host, 'cli-postgres-host')
        self.assertEqual(profile.credentials.port, 6543)
        self.assertEqual(profile.credentials.user, 'env-postgres-user')
        self.assertEqual(profile.credentials.password, 'env-postgres-pass')
        self.assertEqual(profile, from_raw)

    def test_no_profile(self):
        with self.assertRaises(dbt.exceptions.DbtProjectError) as exc:
            self.from_args(project_profile_name=None)
        self.assertIn('no profile was specified', str(exc.exception))

    def test_empty_profile(self):
        self.write_empty_profile()
        with self.assertRaises(dbt.exceptions.DbtProfileError) as exc:
            self.from_args()
        self.assertIn('profiles.yml is empty', str(exc.exception))

    def test_profile_with_empty_profile_data(self):
        renderer = empty_profile_renderer()
        with self.assertRaises(dbt.exceptions.DbtProfileError) as exc:
            dbt.config.Profile.from_raw_profiles(
                self.default_profile_data, 'empty_profile_data', renderer
            )
        self.assertIn(
            'Profile empty_profile_data in profiles.yml is empty',
            str(exc.exception)
        )


class TestProject(BaseConfigTest):
    def setUp(self):
        self.profiles_dir = '/invalid-profiles-path'
        self.project_dir = '/invalid-root-path'
        super().setUp()
        self.default_project_data['project-root'] = self.project_dir

    def test_defaults(self):
        project = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        self.assertEqual(project.project_name, 'my_test_project')
        self.assertEqual(project.version, '0.0.1')
        self.assertEqual(project.profile_name, 'default')
        self.assertEqual(project.project_root, '/invalid-root-path')
        self.assertEqual(project.source_paths, ['models'])
        self.assertEqual(project.macro_paths, ['macros'])
        self.assertEqual(project.data_paths, ['data'])
        self.assertEqual(project.test_paths, ['test'])
        self.assertEqual(project.analysis_paths, [])
        self.assertEqual(project.docs_paths, ['models', 'data', 'snapshots', 'macros'])
        self.assertEqual(project.asset_paths, [])
        self.assertEqual(project.target_path, 'target')
        self.assertEqual(project.clean_targets, ['target'])
        self.assertEqual(project.log_path, 'logs')
        self.assertEqual(project.modules_path, 'dbt_modules')
        self.assertEqual(project.quoting, {})
        self.assertEqual(project.models, {})
        self.assertEqual(project.on_run_start, [])
        self.assertEqual(project.on_run_end, [])
        self.assertEqual(project.seeds, {})
        self.assertEqual(project.dbt_version,
                         [VersionSpecifier.from_version_string('>=0.0.0')])
        self.assertEqual(project.packages, PackageConfig(packages=[]))
        # just make sure str() doesn't crash anything, that's always
        # embarrassing
        str(project)

    def test_eq(self):
        project = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        other = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        self.assertEqual(project, other)

    def test_neq(self):
        project = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        self.assertNotEqual(project, object())

    def test_implicit_overrides(self):
        self.default_project_data.update({
            'source-paths': ['other-models'],
            'target-path': 'other-target',
        })
        project = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        self.assertEqual(project.docs_paths, ['other-models', 'data', 'snapshots', 'macros'])
        self.assertEqual(project.clean_targets, ['other-target'])

    def test_hashed_name(self):
        project = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        self.assertEqual(project.hashed_name(), '754cd47eac1d6f50a5f7cd399ec43da4')

    def test_all_overrides(self):
        self.default_project_data.update({
            'source-paths': ['other-models'],
            'macro-paths': ['other-macros'],
            'data-paths': ['other-data'],
            'test-paths': ['other-test'],
            'analysis-paths': ['analysis'],
            'docs-paths': ['docs'],
            'asset-paths': ['other-assets'],
            'target-path': 'other-target',
            'clean-targets': ['another-target'],
            'log-path': 'other-logs',
            'modules-path': 'other-dbt_modules',
            'quoting': {'identifier': False},
            'models': {
                'pre-hook': ['{{ logging.log_model_start_event() }}'],
                'post-hook': ['{{ logging.log_model_end_event() }}'],
                'my_test_project': {
                    'first': {
                        'enabled': False,
                        'sub': {
                            'enabled': True,
                        }
                    },
                    'second': {
                        'materialized': 'table',
                    },
                },
                'third_party': {
                    'third': {
                        'materialized': 'view',
                    },
                },
            },
            'on-run-start': [
                '{{ logging.log_run_start_event() }}',
            ],
            'on-run-end': [
                '{{ logging.log_run_end_event() }}',
            ],
            'seeds': {
                'my_test_project': {
                    'enabled': True,
                    'schema': 'seed_data',
                    'post-hook': 'grant select on {{ this }} to bi_user',
                },
            },
            'require-dbt-version': '>=0.1.0',
        })
        packages = {
            'packages': [
                {
                    'local': 'foo',
                },
                {
                    'git': 'git@example.com:fishtown-analytics/dbt-utils.git',
                    'revision': 'test-rev'
                },
            ],
        }
        project = dbt.config.Project.from_project_config(
            self.default_project_data, packages
        )
        self.assertEqual(project.project_name, 'my_test_project')
        self.assertEqual(project.version, '0.0.1')
        self.assertEqual(project.profile_name, 'default')
        self.assertEqual(project.project_root, '/invalid-root-path')
        self.assertEqual(project.source_paths, ['other-models'])
        self.assertEqual(project.macro_paths, ['other-macros'])
        self.assertEqual(project.data_paths, ['other-data'])
        self.assertEqual(project.test_paths, ['other-test'])
        self.assertEqual(project.analysis_paths, ['analysis'])
        self.assertEqual(project.docs_paths, ['docs'])
        self.assertEqual(project.asset_paths, ['other-assets'])
        self.assertEqual(project.target_path, 'other-target')
        self.assertEqual(project.clean_targets, ['another-target'])
        self.assertEqual(project.log_path, 'other-logs')
        self.assertEqual(project.modules_path, 'other-dbt_modules')
        self.assertEqual(project.quoting, {'identifier': False})
        self.assertEqual(project.models, {
            'pre-hook': ['{{ logging.log_model_start_event() }}'],
            'post-hook': ['{{ logging.log_model_end_event() }}'],
            'my_test_project': {
                'first': {
                    'enabled': False,
                    'sub': {
                        'enabled': True,
                    }
                },
                'second': {
                    'materialized': 'table',
                },
            },
            'third_party': {
                'third': {
                    'materialized': 'view',
                },
            },
        })
        self.assertEqual(project.on_run_start, ['{{ logging.log_run_start_event() }}'])
        self.assertEqual(project.on_run_end, ['{{ logging.log_run_end_event() }}'])
        self.assertEqual(project.seeds, {
            'my_test_project': {
                'enabled': True,
                'schema': 'seed_data',
                'post-hook': 'grant select on {{ this }} to bi_user',
            },
        })
        self.assertEqual(project.dbt_version,
                         [VersionSpecifier.from_version_string('>=0.1.0')])
        self.assertEqual(
            project.packages,
            PackageConfig(packages=[
                LocalPackage(local='foo'),
                GitPackage(git='git@example.com:fishtown-analytics/dbt-utils.git', revision='test-rev')
            ]))
        str(project)
        json.dumps(project.to_project_config())

    def test_string_run_hooks(self):
        self.default_project_data.update({
            'on-run-start': '{{ logging.log_run_start_event() }}',
            'on-run-end': '{{ logging.log_run_end_event() }}',
        })
        project = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        self.assertEqual(
            project.on_run_start,
            ['{{ logging.log_run_start_event() }}']
        )
        self.assertEqual(
            project.on_run_end,
            ['{{ logging.log_run_end_event() }}']
        )

    def test_invalid_project_name(self):
        self.default_project_data['name'] = 'invalid-project-name'
        with self.assertRaises(dbt.exceptions.DbtProjectError) as exc:
            dbt.config.Project.from_project_config(self.default_project_data, None)

        self.assertIn('invalid-project-name', str(exc.exception))

    def test_no_project(self):
        renderer = empty_project_renderer()
        with self.assertRaises(dbt.exceptions.DbtProjectError) as exc:
            dbt.config.Project.from_project_root(self.project_dir, renderer)

        self.assertIn('no dbt_project.yml', str(exc.exception))

    def test_invalid_version(self):
        self.default_project_data['require-dbt-version'] = 'hello!'
        with self.assertRaises(dbt.exceptions.DbtProjectError):
            dbt.config.Project.from_project_config(self.default_project_data, None)

    def test_unsupported_version(self):
        self.default_project_data['require-dbt-version'] = '>99999.0.0'
        # allowed, because the RuntimeConfig checks, not the Project itself
        dbt.config.Project.from_project_config(self.default_project_data, None)

    def test_none_values(self):
        self.default_project_data.update({
            'models': None,
            'seeds': None,
            'on-run-end': None,
            'on-run-start': None,
        })
        project = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        self.assertEqual(project.models, {})
        self.assertEqual(project.on_run_start, [])
        self.assertEqual(project.on_run_end, [])
        self.assertEqual(project.seeds, {})

    def test_nested_none_values(self):
        self.default_project_data.update({
            'models': {'vars': None, 'pre-hook': None, 'post-hook': None},
            'seeds': {'vars': None, 'pre-hook': None, 'post-hook': None, 'column_types': None},
        })
        project = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        self.assertEqual(project.models, {'vars': {}, 'pre-hook': [], 'post-hook': []})
        self.assertEqual(project.seeds, {'vars': {}, 'pre-hook': [], 'post-hook': [], 'column_types': {}})

    def test_cycle(self):
        models = {}
        models['models'] = models
        self.default_project_data.update({
            'models': models,
        })
        with self.assertRaises(dbt.exceptions.DbtProjectError):
            dbt.config.Project.from_project_config(
                self.default_project_data, None
            )

    def test_query_comment_disabled(self):
        self.default_project_data.update({
            'query-comment': None,
        })
        project = dbt.config.Project.from_project_config(self.default_project_data, None)
        self.assertEqual(project.query_comment.comment, '')
        self.assertEqual(project.query_comment.append, False)

        self.default_project_data.update({
            'query-comment': '',
        })
        project = dbt.config.Project.from_project_config(self.default_project_data, None)
        self.assertEqual(project.query_comment.comment, '')
        self.assertEqual(project.query_comment.append, False)

    def test_default_query_comment(self):
        project = dbt.config.Project.from_project_config(self.default_project_data, None)
        self.assertEqual(project.query_comment, QueryComment())

    def test_default_query_comment_append(self):
        self.default_project_data.update({
            'query-comment': {
                'append': True
            },
        })
        project = dbt.config.Project.from_project_config(self.default_project_data, None)
        self.assertEqual(project.query_comment.comment, DEFAULT_QUERY_COMMENT)
        self.assertEqual(project.query_comment.append, True)

    def test_custom_query_comment_append(self):
        self.default_project_data.update({
            'query-comment': {
                'comment': 'run by user test',
                'append': True
            },
        })
        project = dbt.config.Project.from_project_config(self.default_project_data, None)
        self.assertEqual(project.query_comment.comment, 'run by user test')
        self.assertEqual(project.query_comment.append, True)


class TestProjectFile(BaseFileTest):
    def setUp(self):
        super().setUp()
        self.write_project(self.default_project_data)
        # and after the fact, add the project root
        self.default_project_data['project-root'] = self.project_dir

    def test_from_project_root(self):
        renderer = empty_project_renderer()
        project = dbt.config.Project.from_project_root(self.project_dir, renderer)
        from_config = dbt.config.Project.from_project_config(
            self.default_project_data, None
        )
        self.assertEqual(project, from_config)
        self.assertEqual(project.version, "0.0.1")
        self.assertEqual(project.project_name, 'my_test_project')

    def test_with_invalid_package(self):
        renderer = empty_project_renderer()
        self.write_packages({'invalid': ['not a package of any kind']})
        with self.assertRaises(dbt.exceptions.DbtProjectError):
            dbt.config.Project.from_project_root(self.project_dir, renderer)


class TestRunOperationTask(BaseFileTest):
    def setUp(self):
        super().setUp()
        self.write_project(self.default_project_data)
        self.write_profile(self.default_profile_data)

    def tearDown(self):
        super().tearDown()
        # These tests will change the directory to the project path,
        # so it's necessary to change it back at the end.
        os.chdir(INITIAL_ROOT)

    def test_run_operation_task(self):
        self.assertEqual(os.getcwd(), INITIAL_ROOT)
        self.assertNotEqual(INITIAL_ROOT, self.project_dir)
        new_task = RunOperationTask.from_args(self.args)
        self.assertEqual(os.path.realpath(os.getcwd()),
                         os.path.realpath(self.project_dir))

    def test_run_operation_task_with_bad_path(self):
        self.args.project_dir = 'bad_path'
        with self.assertRaises(dbt.exceptions.RuntimeException):
            new_task = RunOperationTask.from_args(self.args)


class TestVariableProjectFile(BaseFileTest):
    def setUp(self):
        super().setUp()
        self.default_project_data['version'] = "{{ var('cli_version') }}"
        self.default_project_data['name'] = "blah"
        self.default_project_data['profile'] = "{{ env_var('env_value_profile') }}"
        self.write_project(self.default_project_data)
        # and after the fact, add the project root
        self.default_project_data['project-root'] = self.project_dir

    def test_cli_and_env_vars(self):
        renderer = dbt.config.renderer.DbtProjectYamlRenderer(generate_base_context({'cli_version': '0.1.2'}))
        with mock.patch.dict(os.environ, self.env_override):
            project = dbt.config.Project.from_project_root(
                self.project_dir,
                renderer,
            )

        self.assertEqual(project.version, "0.1.2")
        self.assertEqual(project.project_name, 'blah')
        self.assertEqual(project.profile_name, 'default')


class TestRuntimeConfig(BaseConfigTest):
    def setUp(self):
        self.profiles_dir = '/invalid-profiles-path'
        self.project_dir = '/invalid-root-path'
        super().setUp()
        self.default_project_data['project-root'] = self.project_dir

    def get_project(self):
        return dbt.config.Project.from_project_config(
            self.default_project_data, None
        )

    def get_profile(self):
        renderer = empty_profile_renderer()
        return dbt.config.Profile.from_raw_profiles(
            self.default_profile_data, self.default_project_data['profile'], renderer
        )

    def from_parts(self, exc=None):
        project = self.get_project()
        profile = self.get_profile()
        if exc is None:
            return dbt.config.RuntimeConfig.from_parts(project, profile, self.args)

        with self.assertRaises(exc) as err:
            dbt.config.RuntimeConfig.from_parts(project, profile, self.args)
        return err

    def test_from_parts(self):
        project = self.get_project()
        profile = self.get_profile()
        config = dbt.config.RuntimeConfig.from_parts(project, profile, self.args)

        self.assertEqual(config.cli_vars, {})
        self.assertEqual(config.to_profile_info(), profile.to_profile_info())
        # we should have the default quoting set in the full config, but not in
        # the project
        # TODO(jeb): Adapters must assert that quoting is populated?
        expected_project = project.to_project_config()
        self.assertEqual(expected_project['quoting'], {})

        expected_project['quoting'] = {
            'database': True,
            'identifier': True,
            'schema': True,
        }
        self.assertEqual(config.to_project_config(), expected_project)

    def test_str(self):
        project = self.get_project()
        profile = self.get_profile()
        config = dbt.config.RuntimeConfig.from_parts(project, profile, {})

        # to make sure nothing terrible happens
        str(config)

    def test_validate_fails(self):
        project = self.get_project()
        profile = self.get_profile()
        # invalid - must be boolean
        profile.config.use_colors = 100
        with self.assertRaises(dbt.exceptions.DbtProjectError):
            dbt.config.RuntimeConfig.from_parts(project, profile, {})

    def test_supported_version(self):
        self.default_project_data['require-dbt-version'] = '>0.0.0'
        conf = self.from_parts()
        self.assertEqual(set(x.to_version_string() for x in conf.dbt_version), {'>0.0.0'})

    def test_unsupported_version(self):
        self.default_project_data['require-dbt-version'] = '>99999.0.0'
        raised = self.from_parts(dbt.exceptions.DbtProjectError)
        self.assertIn('This version of dbt is not supported', str(raised.exception))

    def test_unsupported_version_no_check(self):
        self.default_project_data['require-dbt-version'] = '>99999.0.0'
        self.args.version_check = False
        conf = self.from_parts()
        self.assertEqual(set(x.to_version_string() for x in conf.dbt_version), {'>99999.0.0'})

    def test_supported_version_range(self):
        self.default_project_data['require-dbt-version'] = ['>0.0.0', '<=99999.0.0']
        conf = self.from_parts()
        self.assertEqual(set(x.to_version_string() for x in conf.dbt_version), {'>0.0.0', '<=99999.0.0'})

    def test_unsupported_version_range(self):
        self.default_project_data['require-dbt-version'] = ['>0.0.0', '<=0.0.1']
        raised = self.from_parts(dbt.exceptions.DbtProjectError)
        self.assertIn('This version of dbt is not supported', str(raised.exception))

    def test_unsupported_version_range_no_check(self):
        self.default_project_data['require-dbt-version'] = ['>0.0.0', '<=0.0.1']
        self.args.version_check = False
        conf = self.from_parts()
        self.assertEqual(set(x.to_version_string() for x in conf.dbt_version), {'>0.0.0', '<=0.0.1'})

    def test_impossible_version_range(self):
        self.default_project_data['require-dbt-version'] = ['>99999.0.0', '<=0.0.1']
        raised = self.from_parts(dbt.exceptions.DbtProjectError)
        self.assertIn('The package version requirement can never be satisfied', str(raised.exception))

    def test_archive_not_allowed(self):
        self.default_project_data['archive'] = [{
            "source_schema": 'a',
            "target_schema": 'b',
            "tables": [
                {
                    "source_table": "seed",
                    "target_table": "archive_actual",
                    "updated_at": 'updated_at',
                    "unique_key": '''id || '-' || first_name'''
                },
            ],
        }]
        with self.assertRaises(dbt.exceptions.DbtProjectError):
            self.get_project()

    def test__no_unused_resource_config_paths(self):
        self.default_project_data.update({
            'models': model_config,
            'seeds': {},
        })
        project = self.from_parts()

        resource_fqns = {'models': model_fqns}
        unused = project.get_unused_resource_config_paths(resource_fqns, [])
        self.assertEqual(len(unused), 0)

    def test__unused_resource_config_paths(self):
        self.default_project_data.update({
            'models': model_config['my_package_name'],
            'seeds': {},
        })
        project = self.from_parts()

        resource_fqns = {'models': model_fqns}
        unused = project.get_unused_resource_config_paths(resource_fqns, [])
        self.assertEqual(len(unused), 3)

    def test__get_unused_resource_config_paths_empty(self):
        project = self.from_parts()
        unused = project.get_unused_resource_config_paths({'models': frozenset((
            ('my_test_project', 'foo', 'bar'),
            ('my_test_project', 'foo', 'baz'),
        ))}, [])
        self.assertEqual(len(unused), 0)

    def test__warn_for_unused_resource_config_paths_empty(self):
        project = self.from_parts()
        dbt.flags.WARN_ERROR = True
        try:
            project.warn_for_unused_resource_config_paths({'models': frozenset((
                ('my_test_project', 'foo', 'bar'),
                ('my_test_project', 'foo', 'baz'),
            ))}, [])
        finally:
            dbt.flags.WARN_ERROR = False


class TestRuntimeConfigWithConfigs(BaseConfigTest):
    def setUp(self):
        self.profiles_dir = '/invalid-profiles-path'
        self.project_dir = '/invalid-root-path'
        super().setUp()
        self.default_project_data['project-root'] = self.project_dir
        self.default_project_data['models'] = {
            'enabled': True,
            'my_test_project': {
                'foo': {
                    'materialized': 'view',
                    'bar': {
                        'materialized': 'table',
                    }
                },
                'baz': {
                    'materialized': 'table',
                }
            }
        }
        self.used = {'models': frozenset((
            ('my_test_project', 'foo', 'bar'),
            ('my_test_project', 'foo', 'baz'),
        ))}

    def get_project(self):
        return dbt.config.Project.from_project_config(
            self.default_project_data, None
        )

    def get_profile(self):
        renderer = empty_profile_renderer()
        return dbt.config.Profile.from_raw_profiles(
            self.default_profile_data, self.default_project_data['profile'], renderer
        )

    def from_parts(self, exc=None):
        project = self.get_project()
        profile = self.get_profile()
        if exc is None:
            return dbt.config.RuntimeConfig.from_parts(project, profile, self.args)

        with self.assertRaises(exc) as err:
            dbt.config.RuntimeConfig.from_parts(project, profile, self.args)
        return err


    def test__get_unused_resource_config_paths(self):
        project = self.from_parts()
        unused = project.get_unused_resource_config_paths(self.used, [])
        self.assertEqual(len(unused), 1)
        self.assertEqual(unused[0], ('models', 'my_test_project', 'baz'))

    @mock.patch.object(dbt.config.runtime, 'warn_or_error')
    def test__warn_for_unused_resource_config_paths(self, warn_or_error):
        project = self.from_parts()
        project.warn_for_unused_resource_config_paths(self.used, [])
        warn_or_error.assert_called_once()

    def test__warn_for_unused_resource_config_paths_disabled(self):
        project = self.from_parts()
        unused = project.get_unused_resource_config_paths(
            self.used,
            frozenset([('my_test_project', 'baz')])
        )

        self.assertEqual(len(unused), 0)


class TestRuntimeConfigFiles(BaseFileTest):
    def setUp(self):
        super().setUp()
        self.write_profile(self.default_profile_data)
        self.write_project(self.default_project_data)
        # and after the fact, add the project root
        self.default_project_data['project-root'] = self.project_dir

    def test_from_args(self):
        with temp_cd(self.project_dir):
            config = dbt.config.RuntimeConfig.from_args(self.args)
        self.assertEqual(config.version, '0.0.1')
        self.assertEqual(config.profile_name, 'default')
        # on osx, for example, these are not necessarily equal due to /private
        self.assertTrue(os.path.samefile(config.project_root,
                                         self.project_dir))
        self.assertEqual(config.source_paths, ['models'])
        self.assertEqual(config.macro_paths, ['macros'])
        self.assertEqual(config.data_paths, ['data'])
        self.assertEqual(config.test_paths, ['test'])
        self.assertEqual(config.analysis_paths, [])
        self.assertEqual(config.docs_paths, ['models', 'data', 'snapshots', 'macros'])
        self.assertEqual(config.asset_paths, [])
        self.assertEqual(config.target_path, 'target')
        self.assertEqual(config.clean_targets, ['target'])
        self.assertEqual(config.log_path, 'logs')
        self.assertEqual(config.modules_path, 'dbt_modules')
        self.assertEqual(config.quoting, {'database': True, 'identifier': True, 'schema': True})
        self.assertEqual(config.models, {})
        self.assertEqual(config.on_run_start, [])
        self.assertEqual(config.on_run_end, [])
        self.assertEqual(config.seeds, {})
        self.assertEqual(config.packages, PackageConfig(packages=[]))
        self.assertEqual(config.project_name, 'my_test_project')


class TestVariableRuntimeConfigFiles(BaseFileTest):
    def setUp(self):
        super().setUp()
        self.default_project_data.update({
            'version': "{{ var('cli_version') }}",
            'name': "blah",
            'profile': "{{ env_var('env_value_profile') }}",
            'on-run-end': [
                "{{ env_var('env_value_profile') }}",
            ],
            'models': {
                'foo': {
                    'post-hook': "{{ env_var('env_value_profile') }}",
                },
                'bar': {
                    # just gibberish, make sure it gets interpreted
                    'materialized': "{{ env_var('env_value_profile') }}",
                }
            },
            'seeds': {
                'foo': {
                    'post-hook': "{{ env_var('env_value_profile') }}",
                },
                'bar': {
                    # just gibberish, make sure it gets interpreted
                    'materialized': "{{ env_var('env_value_profile') }}",
                }
            },
        })
        self.write_project(self.default_project_data)
        self.write_profile(self.default_profile_data)
        # and after the fact, add the project root
        self.default_project_data['project-root'] = self.project_dir

    def test_cli_and_env_vars(self):
        self.args.target = 'cli-and-env-vars'
        self.args.vars = '{"cli_value_host": "cli-postgres-host", "cli_version": "0.1.2"}'
        with mock.patch.dict(os.environ, self.env_override), temp_cd(self.project_dir):
            config = dbt.config.RuntimeConfig.from_args(self.args)

        self.assertEqual(config.version, "0.1.2")
        self.assertEqual(config.project_name, 'blah')
        self.assertEqual(config.profile_name, 'default')
        self.assertEqual(config.credentials.host, 'cli-postgres-host')
        self.assertEqual(config.credentials.user, 'env-postgres-user')
        # make sure hooks are not interpreted
        self.assertEqual(config.on_run_end, ["{{ env_var('env_value_profile') }}"])
        self.assertEqual(config.models['foo']['post-hook'], "{{ env_var('env_value_profile') }}")
        self.assertEqual(config.models['bar']['materialized'], 'default')  # rendered!
        self.assertEqual(config.seeds['foo']['post-hook'], "{{ env_var('env_value_profile') }}")
        self.assertEqual(config.seeds['bar']['materialized'], 'default')  # rendered!


class TestV2V1Conversion(unittest.TestCase):
    def setUp(self):
        self.initial_src_vars = {
            # globals
            'foo': 123,
            'bar': 'hello',
            # project-scoped
            'my_project': {
                'bar': 'goodbye',
                'baz': True,
            },
            'other_project': {
                'foo': 456,
            },
        }
        self.src_vars = deepcopy(self.initial_src_vars)
        self.dst = {'vars': deepcopy(self.initial_src_vars)}

        self.projects = ['my_project', 'other_project', 'third_project']
        load_plugin('postgres')
        self.local_var_search = mock.MagicMock(fqn=['my_project', 'my_model'], resource_type=NodeType.Model, package_name='my_project')
        self.other_var_search = mock.MagicMock(fqn=['other_project', 'model'], resource_type=NodeType.Model, package_name='other_project')
        self.third_var_search = mock.MagicMock(fqn=['third_project', 'third_model'], resource_type=NodeType.Model, package_name='third_project')

    def test_v2_v1_dict(self):
        dbt.config.project.v2_vars_to_v1(self.dst, self.src_vars, self.projects)
        # make sure the input didn't get mutated. That would be bad!
        assert self.src_vars == self.initial_src_vars
        # conversion sould remove top-level 'vars'
        assert 'vars' not in self.dst

        # when we convert, all of models/seeds/snapshots will have the same vars
        for key in ['models', 'seeds', 'snapshots']:
            assert key in self.dst
            for project in self.projects:
                assert project in self.dst[key]
                assert 'vars' in self.dst[key][project]
                if project == 'my_project':
                    assert self.dst[key][project]['vars'] == {
                        'foo': 123,  # override
                        'bar': 'goodbye',
                        'baz': True,  # only in my-project
                    }
                elif project == 'other_project':
                    assert self.dst[key][project]['vars'] == {
                        'foo': 456,  # override
                        'bar': 'hello',
                    }
                elif project == 'third_project':
                    assert self.dst[key][project]['vars'] == {
                        'foo': 123,
                        'bar': 'hello',
                    }
                else:
                    assert False, f'extra project: {project}'

    def test_v2_v1_lookups(self):
        dbt.config.project.v2_vars_to_v1(self.dst, self.src_vars, self.projects)

        v1_vars = dbt.config.project.V1VarProvider(**self.dst)
        v2_vars = dbt.config.project.V2VarProvider(self.initial_src_vars)

        expected = [
            (self.local_var_search, 'foo', 123),
            (self.other_var_search, 'foo', 456),
            (self.third_var_search, 'foo', 123),
            (self.local_var_search, 'bar', 'goodbye'),
            (self.other_var_search, 'bar', 'hello'),
            (self.third_var_search, 'bar', 'hello'),
            (self.local_var_search, 'baz', True),
            (self.other_var_search, 'baz', None),
            (self.third_var_search, 'baz', None),
        ]
        for node, key, expected_value in expected:
            assert v1_vars.vars_for(node, 'postgres').get(key) == expected_value
            assert v2_vars.vars_for(node, 'postgres').get(key) == expected_value
