'''
This is the multiscanner celery worker. To initialize a worker node run:
$ celery -A celery_worker worker
from the utils/ directory.
'''

from datetime import datetime
from socket import gethostname

from celery import Celery, Task
from celery.schedules import crontab
from celery.utils.log import get_task_logger

from kombu import Exchange, Queue

from multiscanner import multiscan, parse_reports
from multiscanner import config as msconf
from multiscanner.storage import elasticsearch_storage, storage
from multiscanner.storage import sql_driver as database
from multiscanner.analytics.ssdeep_analytics import SSDeepAnalytic


logger = get_task_logger(__name__)

DEFAULTCONF = {
    'protocol': 'pyamqp',
    'host': 'localhost',
    'user': 'guest',
    'password': '',
    'vhost': '/',
    'flush_every': '100',
    'flush_interval': '10',
    'tz': 'US/Eastern',
}

configfile = msconf.get_config_path('api')
config = msconf.read_config(configfile, {'celery': DEFAULTCONF, 'Database': database.Database.DEFAULTCONF})
db_config = dict(config.items('Database'))

storage_configfile = msconf.get_config_path('storage')
storage_config = msconf.read_config(storage_configfile)
try:
    es_storage_config = storage_config['ElasticSearchStorage']
except KeyError:
    es_storage_config = {}

default_exchange = Exchange('celery', type='direct')

app = Celery(broker='{0}://{1}:{2}@{3}/{4}'.format(
    config.get('celery', 'protocol'),
    config.get('celery', 'user'),
    config.get('celery', 'password'),
    config.get('celery', 'host'),
    config.get('celery', 'vhost'),
))
app.conf.timezone = config.get('celery', 'tz')
app.conf.task_queues = [
    Queue('low_tasks', default_exchange, routing_key='tasks.low', queue_arguments={'x-max-priority': 10}),
    Queue('medium_tasks', default_exchange, routing_key='tasks.medium', queue_arguments={'x-max-priority': 10}),
    Queue('high_tasks', default_exchange, routing_key='tasks.high', queue_arguments={'x-max-priority': 10}),
]


@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    # Run ssdeep match analytic
    # Executes every morning at 2:00 a.m.
    sender.add_periodic_task(
        crontab(hour=2, minute=0),
        ssdeep_compare_celery.s(),
        **{
            'queue': 'low_tasks',
            'routing_key': 'tasks.low',
            'priority': 1,
        }
    )

    # Delete old metricbeat indices
    # Executes every morning at 3:00 a.m.
    metricbeat_enabled = es_storage_config.get('metricbeat_enabled', True)
    if metricbeat_enabled:
        sender.add_periodic_task(
            crontab(hour=3, minute=0),
            metricbeat_rollover_celery.s(),
            args=(es_storage_config.get('metricbeat_rollover_days'), 7),
            kwargs=dict(config=msconf.MS_CONFIG),
            **{
                'queue': 'low_tasks',
                'routing_key': 'tasks.low',
                'priority': 1,
            }
        )


class MultiScannerTask(Task):
    '''
    Class of tasks that defines call backs to handle signals
    from celery
    '''
    _db = None

    @property
    def db(self):
        if self._db is None:
            self._db = database.Database(config=db_config)
            self._db.init_db()
        return self._db

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        '''
        When a task fails, update the task DB with a "Failed"
        status. Dump a traceback to local logs
        '''
        logger.error('Task #{} failed'.format(args[2]))
        logger.error('Traceback info:\n{}'.format(einfo))

        scan_time = datetime.now().isoformat()

        # Update the task DB with the failure
        self.db.update_task(
            task_id=args[2],
            task_status='Failed',
            timestamp=scan_time,
        )

    def on_success(self, retval, task_id, args, kwargs):
        '''
        When a task succeeds, update the task DB with a "Completed"
        status.
        '''
        logger.info('Completed Task #{}'.format(args[2]))

        # Update the task DB to reflect that the task is done
        self.db.update_task(
            task_id=args[2],
            task_status='Complete',
            timestamp=retval[args[1]]['Scan Metadata']['Scan Time'],
        )


@app.task(base=MultiScannerTask)
def multiscanner_celery(file_, original_filename, task_id, file_hash, metadata,
                        config=None, module_list=None):
    '''
    Queue up multiscanner tasks

    Usage:
    from celery_worker import multiscanner_celery
    multiscanner_celery.delay(full_path, original_filename, task_id,
                              hashed_filename, metadata, config, module_list)
    '''
    logger.info('\n\n{}{}Got file: {}.\nOriginal filename: {}.\n'.format('=' * 48, '\n', file_hash, original_filename))

    # Get the storage config
    if config is None:
        config = msconf.MS_CONFIG
    storage_conf = msconf.get_config_path('storage', config)
    storage_handler = storage.StorageHandler(configfile=storage_conf)

    resultlist = multiscan(
        [file_],
        config=config,
        module_list=module_list
    )
    results = parse_reports(resultlist, python=True)

    scan_time = datetime.now().isoformat()

    # Get the Scan Config that the task was run with and
    # add it to the task metadata
    sub_conf = {}
    # Count number of modules enabled out of total possible (-1 for main)
    # and add it to the Scan Metadata
    total_enabled = 0
    total_modules = len(config.keys()) - 1

    # Get the count of modules enabled from the module_list
    # if it exists, else count via the config
    if module_list:
        total_enabled = len(module_list)
    else:
        for key in config:
            if key == 'main':
                continue
            sub_conf[key] = {}
            sub_conf[key]['ENABLED'] = config[key]['ENABLED']
            if sub_conf[key]['ENABLED'] is True:
                total_enabled += 1

    results[file_]['Scan Metadata'] = metadata
    results[file_]['Scan Metadata']['Worker Node'] = gethostname()
    results[file_]['Scan Metadata']['Scan Config'] = sub_conf
    results[file_]['Scan Metadata']['Modules Enabled'] = '{} / {}'.format(
        total_enabled, total_modules
    )
    results[file_]['Scan Metadata']['Scan Time'] = scan_time
    results[file_]['Scan Metadata']['Task ID'] = task_id

    # Use the original filename as the value for the filename
    # in the report (instead of the tmp path assigned to the file
    # by the REST API)
    results[original_filename] = results[file_]
    del results[file_]

    # Save the reports to storage
    storage_ids = storage_handler.store(results, wait=False)
    storage_handler.close()

    # Only need to raise ValueError here,
    # Further cleanup will be handled by the on_failure method
    # of MultiScannerTask
    if not storage_ids:
        raise ValueError('Report failed to index')

    return results


@app.task()
def ssdeep_compare_celery():
    '''
    Run ssdeep.compare for new samples.

    Usage:
    from celery_worker import ssdeep_compare_celery
    ssdeep_compare_celery.delay()
    '''
    ssdeep_analytic = SSDeepAnalytic()
    ssdeep_analytic.ssdeep_compare()


@app.task()
def metricbeat_rollover_celery(days):
    '''
    Clean up old Elastic Beats indices
    '''
    try:
        # Get the storage config
        storage_handler = storage.StorageHandler(configfile=storage_configfile)
        metricbeat_enabled = es_storage_config.get('metricbeat_enabled', True)

        if not metricbeat_enabled:
            logger.debug('Metricbeat logging not enbaled, exiting...')
            return

        if not days:
            days = es_storage_config.get('metricbeat_rollover_days', 7)
        if not days:
            raise NameError("name 'days' is not defined, check storage.ini for 'metricbeat_rollover_days' setting")

        # Find Elastic storage
        for handler in storage_handler.loaded_storage:
            if isinstance(handler, elasticsearch_storage.ElasticSearchStorage):
                ret = handler.delete_index(index_prefix='metricbeat', days=days)

                if ret is False:
                    logger.warning('Metricbeat Roller failed')
                else:
                    logger.info('Metricbeat indices older than {} days deleted'.format(days))
    except Exception as e:
        logger.warning(e)
    finally:
        storage_handler.close()


if __name__ == '__main__':
    logger.debug('Initializing celery worker...')
    app.start()
