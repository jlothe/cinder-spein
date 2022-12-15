#!/usr/bin/env python
# Copyright (c) 2011 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
    Cron script to generate usage notifications for volumes existing during
    the audit period.

    Together with the notifications generated by volumes
    create/delete/resize, over that time period, this allows an external
    system consuming usage notification feeds to calculate volume usage
    for each tenant.

    Time periods are specified as 'hour', 'month', 'day' or 'year'

    - `hour` - previous hour. If run at 9:07am, will generate usage for
      8-9am.
    - `month` - previous month. If the script is run April 1, it will
      generate usages for March 1 through March 31.
    - `day` -  previous day. if run on July 4th, it generates usages for
      July 3rd.
    - `year` - previous year. If run on Jan 1, it generates usages for
      Jan 1 through Dec 31 of the previous year.

"""

import datetime
import iso8601
import sys

from oslo_config import cfg
from oslo_log import log as logging

from cinder import i18n
i18n.enable_lazy()
from cinder import context
from cinder.i18n import _
from cinder import objects
from cinder import rpc
from cinder import utils
from cinder import version
import cinder.volume.utils


CONF = cfg.CONF
script_opts = [
    cfg.StrOpt('start_time',
               help="If this option is specified then the start time "
                    "specified is used instead of the start time of the "
                    "last completed audit period."),
    cfg.StrOpt('end_time',
               help="If this option is specified then the end time "
                    "specified is used instead of the end time of the "
                    "last completed audit period."),
    cfg.BoolOpt('send_actions',
                default=False,
                help="Send the volume and snapshot create and delete "
                     "notifications generated in the specified period."),
]
CONF.register_cli_opts(script_opts)


def _time_error(LOG, begin, end):
    if CONF.start_time:
        begin = datetime.datetime.strptime(CONF.start_time,
                                           "%Y-%m-%d %H:%M:%S")
    if CONF.end_time:
        end = datetime.datetime.strptime(CONF.end_time,
                                         "%Y-%m-%d %H:%M:%S")
    begin = begin.replace(tzinfo=iso8601.UTC)
    end = end.replace(tzinfo=iso8601.UTC)
    if end <= begin:
        msg = _("The end time (%(end)s) must be after the start "
                "time (%(start)s).") % {'start': begin,
                                        'end': end}
        LOG.error(msg)
        sys.exit(-1)
    return begin, end


def _vol_notify_usage(LOG, volume_ref, extra_info, admin_context):
    """volume_ref notify usage"""
    try:
        LOG.debug("Send exists notification for <volume_id: "
                  "%(volume_id)s> <project_id %(project_id)s> "
                  "<%(extra_info)s>",
                  {'volume_id': volume_ref.id,
                   'project_id': volume_ref.project_id,
                   'extra_info': extra_info})
        cinder.volume.utils.notify_about_volume_usage(
            admin_context, volume_ref, 'exists', extra_usage_info=extra_info)
    except Exception as exc_msg:
        LOG.error("Exists volume notification failed: %s",
                  exc_msg, resource=volume_ref)


def _snap_notify_usage(LOG, snapshot_ref, extra_info, admin_context):
    """snapshot_ref notify usage"""
    try:
        LOG.debug("Send notification for <snapshot_id: %(snapshot_id)s> "
                  "<project_id %(project_id)s> <%(extra_info)s>",
                  {'snapshot_id': snapshot_ref.id,
                   'project_id': snapshot_ref.project_id,
                   'extra_info': extra_info})
        cinder.volume.utils.notify_about_snapshot_usage(
            admin_context, snapshot_ref, 'exists', extra_info)
    except Exception as exc_msg:
        LOG.error("Exists snapshot notification failed: %s",
                  exc_msg, resource=snapshot_ref)


def _backup_notify_usage(LOG, backup_ref, extra_info, admin_context):
    """backup_ref notify usage"""
    try:
        cinder.volume.utils.notify_about_backup_usage(
            admin_context, backup_ref, 'exists', extra_info)
        LOG.debug("Sent notification for <backup_id: %(backup_id)s> "
                  "<project_id %(project_id)s> <%(extra_info)s>",
                  {'backup_id': backup_ref.id,
                   'project_id': backup_ref.project_id,
                   'extra_info': extra_info})
    except Exception as exc_msg:
        LOG.error("Exists backups notification failed: %s", exc_msg)


def _create_action(obj_ref, admin_context, LOG, notify_about_usage,
                   type_id_str, type_name):
    try:
        local_extra_info = {
            'audit_period_beginning': str(obj_ref.created_at),
            'audit_period_ending': str(obj_ref.created_at),
        }
        LOG.debug("Send create notification for <%(type_id_str)s: %(_id)s> "
                  "<project_id %(project_id)s> <%(extra_info)s>",
                  {'type_id_str': type_id_str,
                   '_id': obj_ref.id,
                   'project_id': obj_ref.project_id,
                   'extra_info': local_extra_info})
        notify_about_usage(admin_context, obj_ref,
                           'create.start', extra_usage_info=local_extra_info)
        notify_about_usage(admin_context, obj_ref,
                           'create.end', extra_usage_info=local_extra_info)
    except Exception as exc_msg:
        LOG.error("Create %(type)s notification failed: %(exc_msg)s",
                  {'type': type_name, 'exc_msg': exc_msg}, resource=obj_ref)


def _delete_action(obj_ref, admin_context, LOG, notify_about_usage,
                   type_id_str, type_name):
    try:
        local_extra_info = {
            'audit_period_beginning': str(obj_ref.deleted_at),
            'audit_period_ending': str(obj_ref.deleted_at),
        }
        LOG.debug("Send delete notification for <%(type_id_str)s: %(_id)s> "
                  "<project_id %(project_id)s> <%(extra_info)s>",
                  {'type_id_str': type_id_str,
                   '_id': obj_ref.id,
                   'project_id': obj_ref.project_id,
                   'extra_info': local_extra_info})
        notify_about_usage(admin_context, obj_ref,
                           'delete.start', extra_usage_info=local_extra_info)
        notify_about_usage(admin_context, obj_ref,
                           'delete.end', extra_usage_info=local_extra_info)
    except Exception as exc_msg:
        LOG.error("Delete %(type)s notification failed: %(exc_msg)s",
                  {'type': type_name, 'exc_msg': exc_msg}, resource=obj_ref)


def _obj_ref_action(_notify_usage, LOG, obj_ref, extra_info, admin_context,
                    begin, end, notify_about_usage, type_id_str, type_name):
    _notify_usage(LOG, obj_ref, extra_info, admin_context)
    if CONF.send_actions:
        if begin < obj_ref.created_at < end:
            _create_action(obj_ref, admin_context, LOG,
                           notify_about_usage, type_id_str, type_name)

        if obj_ref.deleted_at and begin < obj_ref.deleted_at < end:
            _delete_action(obj_ref, admin_context, LOG,
                           notify_about_usage, type_id_str, type_name)


def main():
    objects.register_all()
    admin_context = context.get_admin_context()
    CONF(sys.argv[1:], project='cinder',
         version=version.version_string())
    logging.setup(CONF, "cinder")
    LOG = logging.getLogger("cinder")
    rpc.init(CONF)

    begin, end = utils.last_completed_audit_period()
    begin, end = _time_error(LOG, begin, end)

    LOG.info("Starting volume usage audit")
    LOG.info("Creating usages for %(begin_period)s until %(end_period)s",
             {"begin_period": begin, "end_period": end})

    extra_info = {
        'audit_period_beginning': str(begin),
        'audit_period_ending': str(end),
    }

    volumes = objects.VolumeList.get_all_active_by_window(admin_context,
                                                          begin,
                                                          end)

    LOG.info("Found %d volumes", len(volumes))
    for volume_ref in volumes:
        _obj_ref_action(_vol_notify_usage, LOG, volume_ref, extra_info,
                        admin_context, begin, end,
                        cinder.volume.utils.notify_about_volume_usage,
                        "volume_id", "volume")

    snapshots = objects.SnapshotList.get_all_active_by_window(admin_context,
                                                              begin, end)
    LOG.info("Found %d snapshots", len(snapshots))
    for snapshot_ref in snapshots:
        _obj_ref_action(_snap_notify_usage, LOG, snapshot_ref, extra_info,
                        admin_context, begin,
                        end, cinder.volume.utils.notify_about_snapshot_usage,
                        "snapshot_id", "snapshot")

    backups = objects.BackupList.get_all_active_by_window(admin_context,
                                                          begin, end)

    LOG.info("Found %d backups", len(backups))
    for backup_ref in backups:
        _obj_ref_action(_backup_notify_usage, LOG, backup_ref, extra_info,
                        admin_context, begin,
                        end, cinder.volume.utils.notify_about_backup_usage,
                        "backup_id", "backup")
    LOG.info("Volume usage audit completed")
