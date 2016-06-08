"""Module provides target monitoring
metrics collector - influxdata's `telegraf` - https://github.com/influxdata/telegraf/
backward compatibility with yandextank's Monitoring module configuration and tools.
"""

import os
import time
import fnmatch
import datetime
import logging
import json

from ...core.util import expand_to_seconds
from ...core.resource import manager as resource
from ...core.interfaces import MonitoringDataListener, AbstractPlugin, AbstractInfoWidget
from ..Telegraf.collector import MonitoringCollector
from ..Console import ConsolePlugin
from ..Phantom import PhantomPlugin
from ..Autostop import AutostopPlugin, AbstractCriterion

logger = logging.getLogger(__name__)


class Plugin(AbstractPlugin):
    """  resource mon plugin  """

    SECTION = 'telegraf'

    def __init__(self, core):
        AbstractPlugin.__init__(self, core)
        self.jobno = None
        self.default_target = None
        self.config = None
        self.process = None
        self.monitoring = MonitoringCollector()
        self.die_on_fail = True
        self.data_file = None
        self.mon_saver = None
        self.address_resolver = None

    @staticmethod
    def get_key():
        return __file__

    def start_test(self):
        if self.monitoring:
            self.monitoring.load_start_time = time.time()
            logger.debug("load_start_time = %s" %
                         self.monitoring.load_start_time)

    def get_available_options(self):
        return ["config", "default_target", 'ssh_timeout']

    def configure(self):
        self.config = self.get_option("config", 'auto').strip()
        self.default_target = self.get_option("default_target", 'localhost')

        # FIXME [legacy] backward compatibility with Monitoring module configuration below.
        self.monitoring.ssh_timeout = expand_to_seconds(self.get_option(
            'ssh_timeout', "5s"))

        if self.config == 'none' or self.config == 'auto':
            self.die_on_fail = False
        else:
            # FIXME [legacy] handle raw XML config in .ini file
            if self.config[0] == '<':
                xmlfile = self.core.mkstemp(".xml", "monitoring_")
                self.core.add_artifact_file(xmlfile)
                with open(xmlfile, 'w') as f:
                    f.write(self.config)
                self.config = xmlfile
            # handle http/https url or file path
            else:
                filename = resource.resource_filename(self.config)
                xmlfile = self.core.mkstemp(".xml", "monitoring_")
                self.core.add_artifact_file(xmlfile)
                with open(filename, 'rb') as config:
                    config_contents = config.read()
                with open(xmlfile, 'w') as f:
                    f.write(config_contents)
                self.config = filename

        if self.config == 'none':
            self.monitoring = None

        if self.config == 'auto':
            default_config_path = "{}/config/monitoring_default_config.xml".format(
                os.path.dirname(__file__))
            filename = resource.resource_filename(default_config_path)
            with open(filename, 'rb') as default_config:
                default_config_contents = default_config.read()
            self.config = self.core.mkstemp(".xml", "monitoring_default_")
            with open(self.config, 'w') as cfg_file:
                cfg_file.write(default_config_contents)

        try:
            autostop = self.core.get_plugin_of_type(AutostopPlugin)
            autostop.add_criterion_class(MetricHigherCriterion)
            autostop.add_criterion_class(MetricLowerCriterion)
        except KeyError:
            logger.debug(
                "No autostop plugin found, not adding instances criterion")

    def prepare_test(self):
        try:
            phantom = self.core.get_plugin_of_type(PhantomPlugin)
            if phantom.phout_import_mode:
                logger.info("Phout import mode, disabling monitoring")
                self.config = None
                self.monitoring = None

            info = phantom.get_info()
            if info:
                self.default_target = info.address
                logger.debug("Changed monitoring target to %s",
                             self.default_target)
        except KeyError:
            logger.debug("Phantom plugin not found: %s", exc_info=True)

        if self.address_resolver:
            try:
                self.default_target = self.address_resolver.resolve_virtual(
                    self.default_target)
            except Exception, exc:
                logger.error("Failed to get target info: %s", exc)

        if not self.config or self.config == 'none':
            logger.info("Monitoring has been disabled")
        else:
            logger.info("Starting monitoring with config: %s", self.config)
            self.core.add_artifact_file(self.config, True)
            self.monitoring.config = self.config
            if self.default_target:
                self.monitoring.default_target = self.default_target

            self.data_file = self.core.mkstemp('.data', 'monitoring_overall_')
            self.mon_saver = SaveMonToFile(self.data_file)
            self.monitoring.add_listener(self.mon_saver)
            self.core.add_artifact_file(self.data_file)

            try:
                console = self.core.get_plugin_of_type(ConsolePlugin)
            except Exception, ex:
                logger.debug("Console not found: %s", ex)
                console = None
            if console:
                widget = MonitoringWidget(self)
                console.add_info_widget(widget)
                self.monitoring.add_listener(widget)

            try:
                self.monitoring.prepare()
                self.monitoring.start()
                count = 0
                while not self.monitoring.first_data_received and count < 15 * 5:
                    time.sleep(0.2)
                    self.monitoring.poll()
                    count += 1
            except:
                logger.error("Could not start monitoring", exc_info=True)
                if self.die_on_fail:
                    raise
                else:
                    self.monitoring = None

    def is_test_finished(self):
        if self.monitoring:
            data_len = self.monitoring.poll()
            logger.debug("Monitoring got %s lines", data_len)
        return -1

    def end_test(self, retcode):
        logger.info("Finishing monitoring")
        if self.monitoring:
            self.monitoring.stop()
            for log in self.monitoring.artifact_files:
                self.core.add_artifact_file(log)

            while self.monitoring.send_data:
                logger.info("Sending monitoring data rests...")
                self.monitoring.send_collected_data()
        if self.mon_saver:
            self.mon_saver.close()
        return retcode


class SaveMonToFile(MonitoringDataListener):
    """
    Default listener - saves data to file
    """

    def __init__(self, out_file):
        MonitoringDataListener.__init__(self)
        if out_file:
            self.store = open(out_file, 'w')

    def monitoring_data(self, data_string):
        [self.store.write(chunk) for chunk in data_string]
        self.store.flush()

    def close(self):
        """ close open files """
        logger.debug("Closing monitoring file")
        if self.store:
            self.store.close()


class MonitoringWidget(AbstractInfoWidget, MonitoringDataListener):
    """
    Screen widget
    """

    def __init__(self, owner):
        AbstractInfoWidget.__init__(self)
        self.owner = owner
        self.data = {}
        self.sign = {}
        self.time = {}
        self.max_metric_len = 0

    def get_index(self):
        return 50

    def monitoring_data(self, data_string):
        # logger.debug("Mon widget data: %s", data_string)
        for line in data_string:
            if not line.strip(']').strip('['):
                continue
            metrics_json = json.loads(line)
            for hostname in metrics_json[0]['data']:
                host = hostname
                data = metrics_json[0]['data'][hostname]['metrics']
                timestamp = metrics_json[0]['timestamp']
                self.time[host] = timestamp
                self.sign[host] = {}
                self.data[host] = {}
                for metric, value in data.iteritems():
                    self.sign[host][metric] = 0
                    try:
                        self.data[host][metric] = "%.2f" % float(value)
                    except ValueError:
                        self.data[host][metric] = value

    def render(self, screen):
        if not self.owner.monitoring:
            return "Monitoring is " + screen.markup.RED + "offline" + screen.markup.RESET
        else:
            res = "Monitoring is " + screen.markup.GREEN + \
                "online" + screen.markup.RESET + ":\n"
            for hostname, metrics in self.data.items():
                tm_stamp = datetime.datetime.fromtimestamp(float(self.time[
                    hostname])).strftime('%H:%M:%S')
                res += ("   " + screen.markup.CYAN + "%s" + screen.markup.RESET
                        + " at %s:\n") % (hostname, tm_stamp)
                for metric, value in sorted(metrics.iteritems()):
                    if self.sign[hostname][metric] > 0:
                        value = screen.markup.YELLOW + value + screen.markup.RESET
                    elif self.sign[hostname][metric] < 0:
                        value = screen.markup.CYAN + value + screen.markup.RESET
                    res += "      %s%s: %s\n" % (
                        ' ' * (self.max_metric_len - len(metric)),
                        metric.replace('_', ' '), value)

            return res.strip()


class AbstractMetricCriterion(AbstractCriterion, MonitoringDataListener):
    """ Parent class for metric criterion """

    def __init__(self, autostop, param_str):
        AbstractCriterion.__init__(self)
        try:
            self.mon = autostop.core.get_plugin_of_type(Plugin)
            if self.mon.monitoring:
                self.mon.monitoring.add_listener(self)
        except KeyError:
            logger.warning("No monitoring module, mon autostop disabled")
        self.triggered = False
        self.autostop = autostop

        self.host = param_str.split(',')[0].strip()
        self.metric = param_str.split(',')[1].strip()
        self.value_limit = float(param_str.split(',')[2])
        self.seconds_limit = expand_to_seconds(param_str.split(',')[3])
        self.last_second = None
        self.seconds_count = 0

    def monitoring_data(self, data_string):
        if self.triggered:
            return

        for line in data_string:
            if not line.strip(']').strip('['):
                continue

            metrics_json = json.loads(line)
            for hostname in metrics_json[0]['data']:
                host = hostname
                data = metrics_json[0]['data'][hostname]['metrics']
                timestamp = metrics_json[0]['timestamp']

            if not fnmatch.fnmatch(host, self.host):
                continue

            if self.metric not in data.keys() or not data[self.metric]:
                data[self.metric] = 0

            logger.debug("Compare %s %s/%s=%s to %s", self.get_type_string(),
                         host, self.metric, data[self.metric],
                         self.value_limit)
            if self.comparison_fn(float(data[self.metric]), self.value_limit):
                if not self.seconds_count:
                    self.cause_second = self.last_second

                logger.debug(self.explain())

                self.seconds_count += 1
                if self.seconds_count >= self.seconds_limit:
                    logger.debug("Triggering autostop")
                    self.triggered = True
                    return
            else:
                self.seconds_count = 0

    def notify(self, data, stat):
        if self.seconds_count:
            self.autostop.add_counting(self)

        self.last_second = (data, stat)
        return self.triggered

    def comparison_fn(self, arg1, arg2):
        """ comparison function """
        raise NotImplementedError()


class MetricHigherCriterion(AbstractMetricCriterion):
    """ trigger if metric is higher than limit """

    def __init__(self, autostop, param_str):
        AbstractMetricCriterion.__init__(self, autostop, param_str)

    def get_rc(self):
        return 31

    @staticmethod
    def get_type_string():
        return 'metric_higher'

    def explain(self):
        items = (self.host, self.metric, self.value_limit, self.seconds_count)
        return "%s/%s metric value is higher than %s for %s seconds" % items

    def widget_explain(self):
        items = (self.host, self.metric, self.value_limit, self.seconds_count,
                 self.seconds_limit)
        return "%s/%s > %s for %s/%ss" % items, float(
            self.seconds_count) / self.seconds_limit

    def comparison_fn(self, arg1, arg2):
        return arg1 > arg2


class MetricLowerCriterion(AbstractMetricCriterion):
    """ trigger if metric is lower than limit """

    def __init__(self, autostop, param_str):
        AbstractMetricCriterion.__init__(self, autostop, param_str)

    def get_rc(self):
        return 32

    @staticmethod
    def get_type_string():
        return 'metric_lower'

    def explain(self):
        items = (self.host, self.metric, self.value_limit, self.seconds_count)
        return "%s/%s metric value is lower than %s for %s seconds" % items

    def widget_explain(self):
        items = (self.host, self.metric, self.value_limit, self.seconds_count,
                 self.seconds_limit)
        return "%s/%s < %s for %s/%ss" % items, float(
            self.seconds_count) / self.seconds_limit

    def comparison_fn(self, arg1, arg2):
        return arg1 < arg2


# FIXME why is this _here_? move -> ...core.interfaces
class AbstractResolver:
    """ Resolver class provides virtual to real host resolution """

    def __init__(self):
        pass

    def resolve_virtual(self, virt_address):
        """ get host address by virtual """
        raise NotImplementedError()