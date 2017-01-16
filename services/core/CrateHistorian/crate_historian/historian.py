# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:
#
# Copyright (c) 2016, Battelle Memorial Institute
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing
# official policies, either expressed or implied, of the FreeBSD Project.
#

# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor Battelle,
# nor any of their employees, nor any jurisdiction or organization
# that has cooperated in the development of these materials, makes
# any warranty, express or implied, or assumes any legal liability
# or responsibility for the accuracy, completeness, or usefulness or
# any information, apparatus, product, software, or process disclosed,
# or represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does
# not necessarily constitute or imply its endorsement, recommendation,
# r favoring by the United States Government or any agency thereof,
# or Battelle Memorial Institute. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY
# operated by BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830

# }}}
from __future__ import absolute_import, print_function

import hashlib
import logging
import sys
import pytz
from collections import defaultdict
from datetime import datetime

from crate.client.exceptions import ConnectionError
from dateutil.relativedelta import relativedelta
from calendar import monthrange
from datetime import timedelta
from dateutil.tz import tzutc

from crate import client
from zmq.utils import jsonapi

from volttron.platform.agent import utils
from volttron.platform.agent.base_historian import BaseHistorian
from volttron.platform.dbutils.cratedriver import create_schema

utils.setup_logging()
_log = logging.getLogger(__name__)
__version__ = '1.0'


def historian(config_path, **kwargs):
    """
    This method is called by the :py:func:`mongodb.historian.main` to parse
    the passed config file or configuration dictionary object, validate the
    configuration entries, and create an instance of MongodbHistorian

    :param config_path: could be a path to a configuration file or can be a
                        dictionary object
    :param kwargs: additional keyword arguments if any
    :return: an instance of :py:class:`MongodbHistorian`
    """
    if isinstance(config_path, dict):
        config_dict = config_path
    else:
        config_dict = utils.load_config(config_path)
    connection = config_dict.get('connection', None)
    assert connection is not None

    database_type = connection.get('type', None)
    assert database_type is not None

    params = connection.get('params', None)
    assert params is not None

    topic_replacements = config_dict.get('topic_replace_list', None)
    _log.debug('topic_replacements are: {}'.format(topic_replacements))

    CrateHistorian.__name__ = 'CrateHistorian'
    return CrateHistorian(config_dict, topic_replace_list=topic_replacements,
                          **kwargs)


class CrateHistorian(BaseHistorian):
    """
    Historian that stores the data into mongodb collections.

    """

    def __init__(self, config, **kwargs):
        """
        Initialise the historian.

        The historian makes a mongoclient connection to the mongodb server.
        This connection is thread-safe and therefore we create it before
        starting the main loop of the agent.

        In addition, the topic_map and topic_meta are used for caching meta
        data and topics respectively.

        :param kwargs: additional keyword arguments. (optional identity and
                       topic_replace_list used by parent classes)

        """
        super(CrateHistorian, self).__init__(**kwargs)
        self.tables_def, table_names = self.parse_table_def(config)
        self._data_collection = table_names['data_table']
        self._meta_collection = table_names['meta_table']
        self._topic_collection = table_names['topics_table']
        self._agg_topic_collection = table_names['agg_topics_table']
        self._agg_meta_collection = table_names['agg_meta_table']
        self._connection_params = config['connection']['params']
        self._client = None
        self._connection = None

        self._topic_id_map = {}
        self._topic_name_map = {}
        self._topic_meta = {}
        self._agg_topic_id_map = {}

    def publish_to_historian(self, to_publish_list):
        _log.debug("publish_to_historian number of items: {}".format(
            len(to_publish_list)))

        def insert_data(cursor, table_name, topic_id, ts, data):
            insert_query = """INSERT INTO {} (topic_id, ts, result)
                              VALUES(?, ?, ?)
                              ON DUPLICATE KEY UPDATE result=result
                            """.format(table_name)
            _log.debug("QUERY: {}".format(insert_query))
            _log.debug("PARAMS: {}".format(topic_id, ts, data))
            ts_formatted = utils.format_timestamp(ts)

            cursor.execute(insert_query, (topic_id, ts_formatted,
                                          data, data))
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            for x in to_publish_list:
                ts = x['timestamp']
                source = x['source']
                topic = x['topic']
                value = x['value']
                meta = x['meta']

                _log.debug("SOURCE BEFORE!")
                if source == 'scrape':
                    source = 'device'
                if source == 'log':
                    source = 'datalogger'

                meta_type = meta.get('type', None)
                if meta_type is not None:
                    if meta_type == 'integer':
                        value = int(value)
                    elif meta_type == 'float':
                        value = float(value)

                _log.debug('META IS: {}'.format(meta))
                # look at the topics that are stored in the database already
                # to see if this topic has a value
                topic_lower = topic.lower()
                topic_id = hashlib.md5(topic_lower).hexdigest()
                db_topic_name = self._topic_name_map.get(topic_lower, None)

                if db_topic_name is None:
                    cursor.execute("""INSERT INTO topic(id, name)
                                      VALUES(?,?)
                                      ON DUPLICATE KEY UPDATE name=name""",
                                         (topic_id, topic))

                    self._topic_name_map[topic_lower] = topic

                elif db_topic_name != topic:
                    _log.debug('Updating topic: {}'.format(topic))

                    result = cursor.execute(
                        'UPDATE topic set name=? WHERE id=?', (topic, topic_id))
                    self._topic_name_map[topic_lower] = topic

                insert_data(cursor, source, topic_id, ts, value)

                old_meta = self._topic_meta.get(topic_id, {})

                if old_meta.get(topic_id) is None or \
                                str(old_meta.get(topic_id)) != str(meta):
                    _log.debug(
                        'Updating meta for topic: {} {}'.format(topic, meta))
                    meta_insert = """INSERT INTO meta(topic_id, meta_data)
                                     VALUES(?,?)
                                     ON DUPLICATE KEY UPDATE meta_data=meta_data"""
                    cursor.execute(meta_insert, (topic_id, jsonapi.dumps(meta)))
                    self._topic_meta[topic_id] = meta

            self.report_all_handled()
        except ConnectionError:
            _log.error("Cannot connect to crate service.")
            self._connection = None
        finally:
            if cursor is not None:
                cursor.close()
                cursor = None

        #     old_meta = self._topic_meta.get(topic_id, {})
        #     if set(old_meta.items()) != set(meta.items()):
        #         _log.debug(
        #             'Updating meta for topic: {} {}'.format(topic, meta))
        #         meta_cursor.execute()
        #         db[self._meta_collection].insert_one(
        #             {'topic_id': topic_id, 'meta': meta})
        #         self._topic_meta[topic_id] = meta
        #
        #     prefix = topic.split("/")[0]
        #
        #     if prefix == "analysis":
        #         analysis.append((ts, value))
        #     rollup_hour = ts.replace(minute=0, second=0, microsecond=0)
        #     rollup_day = rollup_hour.replace(hour=0)
        #     rollup_month = rollup_day.replace(day=1)
        #
        #     if topic_id is None:
        #         row = db[self._topic_collection].insert_one(
        #             {'topic_name': topic})
        #         topic_id = row.inserted_id
        #         self._topic_id_map[topic_lower] = topic_id
        #         self._topic_name_map[topic_lower] = topic
        #         if int(self.version_nums[0]) >= 2:
        #             # initialize rollup collections
        #             db['hourly_data'].update_one(
        #                 {'ts': rollup_hour, 'topic_id': topic_id},
        #                 {"$setOnInsert": {'ts': rollup_hour,
        #                                   'topic_id': topic_id,
        #                                   'count': 0,
        #                                   'sum': 0,
        #                                   'data': [[]] * 60}},
        #                 upsert=True)
        #             db['daily_data'].update_one(
        #                 {'ts': rollup_day, 'topic_id': topic_id},
        #                 {"$setOnInsert": {'ts': rollup_day,
        #                                   'topic_id': topic_id,
        #                                   'count': 0,
        #                                   'sum': 0,
        #                                   'data': [[]] * 24 * 60}},
        #                 upsert=True)
        #             weekday, num_days = monthrange(rollup_month.year,
        #                                            rollup_month.month)
        #             db['monthly_data'].update_one(
        #                 {'ts': rollup_month, 'topic_id': topic_id},
        #                 {"$setOnInsert": {'ts': rollup_month,
        #                                   'topic_id': topic_id,
        #                                   'count': 0,
        #                                   'sum': 0,
        #                                   'data': [[]]*num_days*24*60}},
        #                 upsert=True)
        #
        #             _log.debug("After init of rollup rows for new topic {} "
        #                        "hr-{} day-{} month-{}".format(db_topic_name,
        #                                                       rollup_day,
        #                                                       rollup_hour,
        #                                                       rollup_month))
        #
        #     elif db_topic_name != topic:
        #         _log.debug('Updating topic: {}'.format(topic))
        #
        #         result = db[self._topic_collection].update_one(
        #             {'_id': ObjectId(topic_id)},
        #             {'$set': {'topic_name': topic}})
        #         assert result.matched_count
        #         self._topic_name_map[topic_lower] = topic
        #
        #     old_meta = self._topic_meta.get(topic_id, {})
        #     if set(old_meta.items()) != set(meta.items()):
        #         _log.debug(
        #             'Updating meta for topic: {} {}'.format(topic, meta))
        #         db[self._meta_collection].insert_one(
        #             {'topic_id': topic_id, 'meta': meta})
        #         self._topic_meta[topic_id] = meta
        #
        #     # Reformat to a filter tha bulk inserter.
        #     bulk_publish.append(ReplaceOne(
        #         {'ts': ts, 'topic_id': topic_id},
        #         {'ts': ts, 'topic_id': topic_id, 'value': value},
        #         upsert=True))
        #     if int(self.version_nums[0]) >= 2:
        #         bulk_publish_hour.append(UpdateOne(
        #             {'ts': rollup_hour, 'topic_id': topic_id},
        #             {'$push': {"data."+ str(ts.minute) :
        #                            {'$each': [ts, value]}},
        #              '$inc':{'count': 1, 'sum': value}}
        #         ))
        #
        #         position = ts.hour * 60 + ts.minute
        #         bulk_publish_day.append(UpdateOne(
        #             {'ts': rollup_day, 'topic_id': topic_id},
        #             {'$push': {"data." + str(position):
        #                            {'$each': [ts, value]}},
        #              '$inc': {'count': 1, 'sum': value}}))
        #
        #         position = (ts.day * 24 * 60) + (ts.hour * 60) + ts.minute
        #         bulk_publish_month.append(UpdateOne(
        #             {'ts': rollup_month, 'topic_id': topic_id},
        #             {'$push':{"data." + str(position):
        #                           {'$each': [ts, value]}},
        #              '$inc': {'count': 1, 'sum': value}}))
        #
        #
        # # done going through all data and adding appropriate updates stmts
        # # perform bulk write into data and roll up collections
        # _log.debug("bulk_publish_hour {}".format(bulk_publish_hour))
        # _log.debug("bulk_publish_day {}".format(bulk_publish_day))
        # _log.debug("bulk_publish_month {}".format(bulk_publish_month))
        # try:
        #     # http://api.mongodb.org/python/current/api/pymongo
        #     # /collection.html#pymongo.collection.Collection.bulk_write
        #     result = db[self._data_collection].bulk_write(bulk_publish)
        # except BulkWriteError as bwe:
        #     _log.error("Error during bulk write to data: {}".format(
        #         bwe.details))
        # else:  # No write errros here when
        #     if not result.bulk_api_result['writeErrors']:
        #         self.report_all_handled()
        #     else:
        #         # TODO handle when something happens during writing of
        #         # data.
        #         _log.error('SOME THINGS DID NOT WORK')
        #
        # if int(self.version_nums[0]) >= 2:
        #     try:
        #         # http://api.mongodb.org/python/current/api/pymongo
        #         # /collection.html#pymongo.collection.Collection.bulk_write
        #         result = db['hourly_data'].bulk_write(bulk_publish_hour)
        #     except BulkWriteError as bwe:
        #         _log.error("Error during bulk write to hourly data:{}".format(
        #             bwe.details))
        #
        #     try:
        #         # http://api.mongodb.org/python/current/api/pymongo
        #         # /collection.html#pymongo.collection.Collection.bulk_write
        #         result = db['daily_data'].bulk_write(bulk_publish_day)
        #     except BulkWriteError as bwe:
        #         _log.error("Error during bulk write to daily data:{}".format(
        #             bwe.details))
        #
        #     try:
        #         # http://api.mongodb.org/python/current/api/pymongo
        #         # /collection.html#pymongo.collection.Collection.bulk_write
        #         result = db['monthly_data'].bulk_write(bulk_publish_month)
        #     except BulkWriteError as bwe:
        #         _log.error("Error during bulk write to monthly data:{}".format(
        #             bwe.details))

    def query_historian(self, topic, start=None, end=None, agg_type=None,
                        agg_period=None, skip=0, count=None,
                        order="FIRST_TO_LAST"):
        """ Returns the results of the query from the mongo database.

        This historian stores data to the nearest second.  It will not
        store subsecond resolution data.  This is an optimisation based
        upon storage for the database.
        Please see
        :py:meth:`volttron.platform.agent.base_historian.BaseQueryHistorianAgent.query_historian`
        for input parameters and return value details
        """
        #try:
        # TODO make sure to handle data
        table_name = topic.split('/')[0]
        results = []

        if not table_name in ('analysis', 'device', 'record', 'datalogger'):
            _log.error("Invalid topic {}".format(topic))
            return dict(values=results, metadata={})

        if table_name in ('analysis', 'device'):
            topic = topic[len(table_name)+1:]

        if topic not in self._topic_id_map:
            _log.error('Invalid topic {}')
            return dict(values=results, metadata={})

        topic_id = self._topic_id_map[topic]

        #  start_time = datetime.utcnow()
        if start is not None:
            start_time = start

        query = """SELECT topic_id, ts, result
                    FROM """ + table_name + """
                        {where}
                        {order_by}
                        {limit}
                        {offset}"""
        # topics_list = []
        # if isinstance(topic, str):
        #     topics_list.append(topic)
        # elif isinstance(topic, list):
        #     topics_list = topic
        #
        #

        start_time = start
        if start is not None:
            start_time = utils.parse_timestamp_string(start)
        else:
            start_time = utils.get_aware_utc_now()

        start_time = start_time.isoformat('T')

        if end is not None:
            end_time = utils.parse_timestamp_string(end)

        args = dict(where='', order_by='', limit=20, offset='')
        values = []

        args['where'] = " WHERE topic_id = ?"
        values.append(topic_id)

        if start is not None and end is not None:
            args['where'] = " AND ts BETWEEN ? and ?"
            values.append(start_time, end_time)

        if order == "FIRST_TO_LAST":
            args['order_by'] = " ORDER BY ts ASC"
        else:
            args['order_by'] = " ORDER BY ts DESC"

        if skip > 0:
            args['offset'] = " SKIP {}".format(int(skip))

        if count > 0:
            args['limit'] = " LIMIT {}".format(int(count))

        _log.debug("QUERY iS: {}".format(query))
        real_query = query.format(**args)

        _log.debug("REAL QUERY: {}".format(real_query))
        _log.debug("PARAMS: {}".format(values))

        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(real_query, values)

        results = []

        for row in cursor.fetchall():
            results.append((row[1], row[2]))

        return dict(values=results, metadata={})

        # topic_ids = []
        # id_name_map = {}
        # for topic in topics_list:
        #     # find topic if based on topic table entry
        #     topic_id = self._topic_id_map.get(topic.lower(), None)
        #
        #     if agg_type:
        #         agg_type = agg_type.lower()
        #         # replace id from aggregate_topics table
        #         topic_id = self._agg_topic_id_map.get(
        #             (topic.lower(), agg_type, agg_period), None)
        #         if topic_id is None:
        #             # load agg topic id again as it might be a newly
        #             # configured aggregation
        #             self._agg_topic_id_map = mongoutils.get_agg_topic_map(
        #                 self._client, self._agg_topic_collection)
        #             topic_id = self._agg_topic_id_map.get(
        #                 (topic.lower(), agg_type, agg_period), None)
        #     if topic_id:
        #         topic_ids.append(topic_id)
        #         id_name_map[ObjectId(topic_id)] = topic
        #     else:
        #         _log.warn('No such topic {}'.format(topic))
        #
        # if not topic_ids:
        #     return {}
        # else:
        #     _log.debug("Found topic id for {} as {}".format(
        #         topics_list, topic_ids))
        # multi_topic_query = len(topic_ids) > 1
        # db = self._client.get_default_database()
        #
        # ts_filter = {}
        # order_by = 1
        # if order == 'LAST_TO_FIRST':
        #     order_by = -1
        #
        # if start is not None:
        #     if use_rolled_up_data:
        #         ts_filter["$gte"] = query_start
        #     else:
        #         ts_filter["$gte"] = start
        # if end is not None:
        #     if use_rolled_up_data:
        #         ts_filter["$lt"] = query_end
        #     else:
        #         ts_filter["$lt"] = end
        #
        # if count is None:
        #     count = 100
        # skip_count = 0
        # if skip > 0:
        #     skip_count = skip
        #
        # find_params = {}
        # if ts_filter:
        #     if start == end :
        #         find_params = {'ts' : start}
        #     else:
        #         find_params = {'ts': ts_filter}
        #
        # values = defaultdict(list)
        # for x in topic_ids:
        #     find_params['topic_id'] = ObjectId(x)
        #     _log.debug("querying table with params {}".format(find_params))
        #     if use_rolled_up_data:
        #         project = {"_id": 0, "data": 1}
        #     else:
        #         project = {"_id": 0, "timestamp": {
        #         '$dateToString': {'format': "%Y-%m-%dT%H:%M:%S.%L000+00:00",
        #             "date": "$ts"}}, "value": 1}
        #     pipeline = [{"$match": find_params}, {"$skip": skip_count},
        #                 {"$sort": {"ts": order_by}}, {"$limit": count}, {
        #                     "$project": project}]
        #     _log.debug("pipeline for agg query is {}".format(pipeline))
        #     _log.debug("collection_name is "+ collection_name)
        #     cursor = db[collection_name].aggregate(pipeline)
        #
        #     rows = list(cursor)
        #     _log.debug("Time after fetch {}".format(
        #         datetime.utcnow() - start_time))
        #     if use_rolled_up_data:
        #         for row in rows:
        #             for data in row['data']:
        #                 if data:
        #                     _log.debug("start {}".format(start))
        #                     _log.debug("end {}".format(end))
        #                     if start.tzinfo:
        #                         data[0] = data[0].replace(tzinfo=tzutc())
        #                     _log.debug("data[0] {}".format(data[0]))
        #                     if data[0] >= start and data[0] < end:
        #                         values[id_name_map[x]].append(
        #                             (utils.format_timestamp(data[0]),
        #                              data[1]))
        #         _log.debug("values len {}".format(len(values)))
        #     else:
        #         for row in rows:
        #             values[id_name_map[x]].append(
        #                 (row['timestamp'], row['value']))
        #     _log.debug("Time taken to load into values {}".format(
        #         datetime.utcnow() - start_time))
        #     _log.debug("rows length {}".format(len(rows)))
        #
        # _log.debug("Time taken to load all values {}".format(
        #     datetime.utcnow() - start_time))
        # #_log.debug("Results got {}".format(values))
        #
        # if len(values) > 0:
        #     # If there are results add metadata if it is a query on a
        #     # single
        #     # topic
        #     if not multi_topic_query:
        #         values = values.values()[0]
        #         if agg_type:
        #             # if aggregation is on single topic find the topic id
        #             # in the topics table.
        #             _log.debug("Single topic aggregate query. Try to get "
        #                        "metadata")
        #             topic_id = self._topic_id_map.get(topic.lower(), None)
        #             if topic_id:
        #                 _log.debug("aggregation of a single topic, "
        #                            "found topic id in topic map. "
        #                            "topic_id={}".format(topic_id))
        #                 metadata = self._topic_meta.get(topic_id, {})
        #             else:
        #                 # if topic name does not have entry in topic_id_map
        #                 # it is a user configured aggregation_topic_name
        #                 # which denotes aggregation across multiple points
        #                 metadata = {}
        #         else:
        #             # this is a query on raw data, get metadata for
        #             # topic from topic_meta map
        #             _log.debug("Single topic regular query. Get "
        #                        "metadata from meta map for {}".format(
        #                 topic_ids[0]))
        #             metadata = self._topic_meta.get(topic_ids[0], {})
        #             _log.debug("Metadata found {}".format(metadata))
        #         return {'values': values, 'metadata': metadata}
        #     else:
        #         _log.debug("return values without metadata for multi "
        #                    "topic query")
        #         return {'values': values}
        # else:
        #     return {}

    def query_topic_list(self):
        pass
        #
        # db = self._client.get_default_database()
        # cursor = db[self._topic_collection].find()
        #
        # res = []
        # for document in cursor:
        #     res.append(document['topic_name'])
        #
        # return res

    def query_topics_metadata(self, topics):
        pass
        # meta = {}
        # if isinstance(topics, str):
        #     topic_id = self._topic_id_map.get(topics.lower())
        #     if topic_id:
        #         meta = {topics: self._topic_meta.get(topic_id)}
        # elif isinstance(topics, list):
        #     for topic in topics:
        #         topic_id = self._topic_id_map.get(topic.lower())
        #         if topic_id:
        #             meta[topic] = self._topic_meta.get(topic_id)
        # return meta

    def query_aggregate_topics(self):
        pass

        # return mongoutils.get_agg_topics(
        #     self._client,
        #     self._agg_topic_collection,
        #     self._agg_meta_collection)

    def _load_topic_map(self):
        _log.debug('loading topic map')
        cursor = self._connection.cursor()

        cursor.execute("""
            SELECT _id, name, lower(name) AS lower_name
            FROM topic
        """)

        for row in cursor.fetchall():
            self._topic_id_map[row[2]] = row[0]
            self._topic_name_map[row[2]] = row[1]

        cursor.close()

    def _load_meta_map(self):
        _log.debug('loading meta map')
        cursor = self._connection.cursor()

        cursor.execute("""
            SELECT topic_id, meta_data
            FROM meta
        """)

        for row in cursor.fetchall():
            self._topic_meta[row[0]] = jsonapi.loads(row[1])

        cursor.close()

    def get_connection(self):
        if self._connection is None:
            self._connection = client.connect(self._connection_params['host'],
                                              error_trace=True)
        return self._connection

    def historian_setup(self):
        _log.debug("HISTORIAN SETUP")

        self._connection = self.get_connection()

        create_schema(self._connection)

        self._load_topic_map()
        self._load_meta_map()

        # self._client = mongoutils.get_mongo_client(self._connection_params)
        # db = self._client.get_default_database()
        # db[self._data_collection].create_index(
        #     [('topic_id', pymongo.DESCENDING), ('ts', pymongo.DESCENDING)],
        #     unique=True, background=True)

        # self._topic_id_map, self._topic_name_map = \
        #     mongoutils.get_topic_map(
        #         self._client, self._topic_collection)
        # self._load_meta_map()
        #
        # if self._agg_topic_collection in db.collection_names():
        #     _log.debug("found agg_topics_collection ")
        #     self._agg_topic_id_map = mongoutils.get_agg_topic_map(
        #         self._client, self._agg_topic_collection)
        # else:
        #     _log.debug("no agg topics to load")
        #     self._agg_topic_id_map = {}

    def record_table_definitions(self, meta_table_name):
        _log.debug("In record_table_def  table:{}".format(meta_table_name))
        pass
        #
        # db = self._client.get_default_database()
        # db[meta_table_name].bulk_write([
        #     ReplaceOne(
        #         {'table_id': 'data_table'},
        #         {'table_id': 'data_table',
        #          'table_name': self._data_collection, 'table_prefix': ''},
        #         upsert=True),
        #     ReplaceOne(
        #         {'table_id': 'topics_table'},
        #         {'table_id': 'topics_table',
        #          'table_name': self._topic_collection, 'table_prefix': ''},
        #         upsert=True),
        #     ReplaceOne(
        #         {'table_id': 'meta_table'},
        #         {'table_id': 'meta_table',
        #          'table_name': self._meta_collection, 'table_prefix': ''},
        #         upsert=True)])



def main(argv=sys.argv):
    """Main method called by the eggsecutable.
    @param argv:
    """
    try:
        utils.vip_main(historian)
    except Exception as e:
        print(e)
        _log.exception('unhandled exception')


if __name__ == '__main__':
    # Entry point for script
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
