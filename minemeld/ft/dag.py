from __future__ import absolute_import

import logging
import yaml
import netaddr
import gevent
import gevent.queue
import os
import re

import pan.xapi

from . import base
from . import table
from .utils import utc_millisec

LOG = logging.getLogger(__name__)

SUBRE = re.compile("^[A-Za-z0-9_]")


class DevicePusher(gevent.Greenlet):
    def __init__(self, device, prefix, watermark, attributes):
        super(DevicePusher, self).__init__()

        self.device = device
        self.xapi = pan.xapi.PanXapi(
            tag=self.device.get('tag', None),
            api_username=self.device.get('api_username', None),
            api_password=self.device.get('api_password', None),
            port=self.device.get('port', None),
            hostname=self.device.get('hostname', None),
            serial=self.device.get('serial', None)
        )

        self.prefix = prefix
        self.attributes = attributes
        self.watermark = watermark

        self.q = gevent.queue.Queue()

    def put(self, op, address, value):
        LOG.debug('adding %s:%s to device queue', op, address)
        self.q.put([op, address, value])

    def _get_all_registered_ips(self):
        self.xapi.op(
            cmd='show object registered-ip all',
            vsys=self.device.get('vsys', None),
            cmd_xml=True
        )

        entries = self.xapi.element_root.findall('./result/entry')
        if not entries:
            return None

        addresses = {}
        for entry in entries:
            ip = entry.get("ip")

            members = entry.findall("./tag/member")

            tags = [member.text for member in members
                    if member.text and member.text.startswith(self.prefix)]

            if len(tags) > 0:
                addresses[ip] = (tags if len(tags) != members else None)

        return addresses

    def _dag_message(self, type_, addresses):
        message = [
            "<uid-message>",
            "<version>1.0</version>",
            "<type>update</type>",
            "<payload>"
        ]
        message.append('<%s>' % type_)

        if addresses is not None and len(addresses) != 0:
            for a, tags in addresses.iteritems():
                message.append('<entry ip="%s">' % a)

                if tags is not None:
                    message.append('<tag>')
                    for t in tags:
                        message.append('<member>%s</member>' % t)
                    message.append('</tag>')

                message.append('</entry>')

        message.append('</%s>' % type_)
        message.append('</payload></uid-message>')

        return ''.join(message)

    def _clear_registered_addresses(self):
        while True:
            try:
                addresses = self._get_all_registered_ips()
                msg = self._dag_message('unregister', addresses)
                self.xapi.user_id(cmd=msg)

            except gevent.GreenletExit:
                raise

            except:
                LOG.exception('exception clear registered ips in %s',
                              self.device)
                gevent.sleep(60)

            else:
                break

    def _push(self, op, address, value):
        tags = []

        tags.append('%s%s' % (self.prefix, self.watermark))

        for t in self.attributes:
            if t in value:
                if type(value[t]) == unicode:
                    v = value[t].encode('ascii', 'replace')
                else:
                    v = str(value[t])
                v = SUBRE.sub('_', v)

                tag = '%s%s_%s' % (self.prefix, t, str(value[t]))
                tags.append(tag)

        if len(tags) == 0:
            tags = None

        msg = self._dag_message(op, {address: tags})

        self.xapi.user_id(cmd=msg)

    def _run(self):
        self._clear_registered_addresses()

        while True:
            try:
                op, address, value = self.q.get()
                LOG.debug('got item')
                self._push(op, address, value)

            except gevent.GreenletExit:
                break

            except pan.xapi.PanXapiError as e:
                if 'already exists, ignore' not in e.message:
                    LOG.exception('exception in pusher for device %s',
                                  self.device)
                    gevent.sleep(60)

            except:
                LOG.exception('exception in pusher for device %s',
                              self.device)
                gevent.sleep(60)


class DagPusher(base.BaseFT):
    def __init__(self, name, chassis, config):
        self.devices = []
        self.device_pushers = []

        self.device_list_glet = None
        self.device_list_mtime = None

        self.ageout_glet = None
        self.last_ageout_run = None

        super(DagPusher, self).__init__(name, chassis, config)

    def configure(self):
        super(DagPusher, self).configure()

        self.device_list_path = self.config.get('device_list', None)
        self.age_out = self.config.get('age_out', 3600)
        self.age_out_interval = self.config.get('age_out_interval', None)
        self.tag_prefix = self.config.get('tag_prefix', 'mmld_')
        self.tag_watermark = self.config.get('tag_watermark', 'pushed')
        self.tag_attributes = self.config.get(
            'tag_attributes',
            ['confidence']
        )

    def _initialize_table(self, truncate=False):
        self.table = table.Table(self.name, truncate=truncate)
        self.table.create_index('_age_out')

    def initialize(self):
        self._initialize_table()

    def rebuild(self):
        self.rebuild_flag = True
        self._initialize_table(truncate=(self.last_checkpoint is None))

    def reset(self):
        self._initialize_table(truncate=True)

    @base._counting('update.processed')
    def filtered_update(self, source=None, indicator=None, value=None):
        if value.get('type', None) not in ['IPv4', 'IPv6']:
            return

        try:
            address = netaddr.IPAddress(indicator)
        except ValueError:
            LOG.error('%s - invalid IP address received, ignored',
                      self.name)
            return

        if address.netmask_bits() != 32:
            LOG.error('%s - IP network received, ignored',
                      self.name)
            return

        current_value = self.table.get(str(address))

        now = utc_millisec()
        age_out = now+self.age_out*1000

        value['_age_out'] = age_out

        self.table.put(str(address), value)

        value.pop('_age_out')

        uflag = False
        if current_value is not None:
            for t in self.tag_attributes:
                cv = current_value.get(t, None)
                nv = value.get(t, None)
                uflag |= cv != nv

        for p in self.device_pushers:
            if uflag:
                p.put('unregister', str(address), current_value)
            p.put('register', str(address), value)

    @base._counting('withdraw.processed')
    def filtered_withdraw(self, source=None, indicator=None, value=None):
        try:
            address = netaddr.IPAddress(indicator)
        except ValueError:
            LOG.error('%s - invalid IP address received, ignored',
                      self.name)
            return

        if address.netmask_bits() != 32:
            LOG.error('%s - IP network received, ignored',
                      self.name)
            return

        current_value = self.table.get(str(address))
        if current_value is None:
            LOG.debug('%s - unknown indicator received, ignored', self.name)
            return

        current_value.pop('_age_out', None)

        self.table.delete(str(address))
        for p in self.device_pushers:
            p.put('unregister', str(indicator), current_value)

    def _age_out_run(self):
        while True:
            try:
                now = utc_millisec()

                LOG.debug('now: %s', now)

                for i, v in self.table.query(index='_age_out',
                                             to_key=now-1,
                                             include_value=True):
                    LOG.debug('%s - %s %s aged out', self.name, i, v)

                    for dp in self.device_pushers:
                        dp.put(
                            op='unregister',
                            address=i,
                            value=v
                        )

                    self.table.delete(i)

                self.last_ageout_run = now

            except gevent.GreenletExit:
                break

            except:
                LOG.exception('Exception in _age_out_loop')

            try:
                gevent.sleep(self.age_out_interval)
            except gevent.GreenletExit:
                break

    def _spawn_device_pusher(self, device):
        dp = DevicePusher(
            device,
            self.tag_prefix,
            self.tag_watermark,
            self.tag_attributes
        )
        dp.link_exception(self._device_puhser_died)

        for i, v in self.table.query(include_value=True):
            dp.put('register', i, v)

        return dp

    def _device_puhser_died(self, g):
        try:
            g.get()

        except gevent.GreenletExit:
            pass

        except:
            LOG.exception('%s - exception in greenlet for %s, '
                          'respawning in 60 seconds',
                          self.name, g.device)

            idx = self.device_pushers.index(g.device)
            dp = self._spawn_device_pusher(g.device)
            self.device_pushers[idx] = dp
            dp.start_later(60)

    def _load_device_list(self):
        with open(self.device_list_path, 'r') as dlf:
            dlist = yaml.safe_load(dlf)

        added = [d for i, d in enumerate(dlist) if d not in self.devices]
        removed = [i for i, d in enumerate(self.devices) if d not in dlist]

        dpushers = []
        for d in dlist:
            if d in added:
                dp = self._spawn_device_pusher(d)
                dpushers.append(dp)
            else:
                idx = self.devices.index(d)
                dpushers.append(self.device_pushers[idx])

        for idx in removed:
            self.device_pushers[idx].kill()

        self.device_pushers = dpushers
        self.devices = dlist

        for g in self.device_pushers:
            if g.value is None and not g.started:
                g.start()

    def _device_list_monitor(self):
        if self.device_list_path is None:
            LOG.warning('%s - no device_list path configured', self.name)
            return

        while True:
            try:
                mtime = os.stat(self.device_list_path).st_mtime
            except:
                LOG.debug('%s - error checking mtime of %s',
                          self.name, self.device_list_path)
                gevent.sleep(10)
                continue

            if mtime != self.device_list_mtime:
                self.device_list_mtime = mtime

                try:
                    self._load_device_list()
                    LOG.info('%s - device list loaded', self.name)
                except:
                    LOG.exception('%s - exception loading device list')

            gevent.sleep(5)

    def mgmtbus_status(self):
        result = super(DagPusher, self).mgmtbus_status()

        result['devices'] = len(self.devices)

        return result

    def length(self, source=None):
        return self.table.num_indicators

    def start(self):
        super(DagPusher, self).start()

        if self.device_list_glet is not None:
            return

        self.device_list_glet = gevent.spawn_later(
            2,
            self._device_list_monitor
        )

        if self.age_out_interval is not None:
            self.ageout_glet = gevent.spawn(self._age_out_run)

    def stop(self):
        super(DagPusher, self).stop()

        if self.device_list_glet is None:
            return

        for g in self.device_pushers:
            g.kill()

        self.device_list_glet.kill()

        if self.ageout_glet is not None:
            self.ageout_glet.kill()
