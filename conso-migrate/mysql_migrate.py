"""
Migrate a legacy MySQL platform database to the ConSo schema.

ConSo schema: one database per id_platform, one table per country prefix.
Source and target MySQL credentials are read from S3.

Usage:
    python mysql_migrate.py \
        --id-platform DRD \
        --orig-db doordash_rb \
        --orig-table feeds \
        --id-outlet-field id_outlet \
        --country-field country \
        --source-config-key config/doordash/config.json \
        [--target-config-key config/ubereats/config.json] \
        [--dry-run]
"""

import argparse
import json

import boto3
import pandas as pd
import pymysql

from contextlib import closing
from sqlalchemy import (
    Boolean, Column, DateTime, Float, MetaData,
    String, Table, Text, create_engine,
)
from sqlalchemy.sql import text


def load_config(s3_key: str) -> dict:
    s3 = boto3.resource('s3')
    obj = s3.Object('dash-dbcenter', s3_key)
    return json.loads(obj.get()['Body'].read())


def connect(config: dict) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config['host'],
        user=config['user'],
        password=config['passwd'],
        charset='utf8mb4',
    )


def mysql_type_to_sa(type_str: str):
    if type_str.startswith('varchar'):   return String(255)
    if type_str.startswith('tinyint'):   return Boolean
    if type_str.startswith('float'):     return Float
    if type_str.startswith('text'):      return Text
    if type_str.startswith(('datetime', 'timestamp')): return DateTime
    return Text


def main():
    parser = argparse.ArgumentParser(description='Migrate legacy MySQL DB to ConSo schema.')
    parser.add_argument('--id-platform',          required=True)
    parser.add_argument('--orig-db',              required=True, dest='orig_db')
    parser.add_argument('--orig-table',           required=True, dest='orig_table')
    parser.add_argument('--id-outlet-field',      required=True, dest='id_outlet_field')
    parser.add_argument('--country-field',        required=True, dest='country_field')
    parser.add_argument('--source-config-key',    required=True, dest='source_config_key')
    parser.add_argument('--target-config-key',    default='config/ubereats/config.json',
                        dest='target_config_key')
    parser.add_argument('--dry-run',              action='store_true',
                        help='Show what would be migrated without writing anything')
    args = parser.parse_args()

    source_cfg = load_config(args.source_config_key)
    target_cfg = load_config(args.target_config_key)

    with connect(source_cfg) as src_conn:
        src_conn.select_db(args.orig_db)

        with closing(src_conn.cursor()) as cur:
            cur.execute(f'DESCRIBE {args.orig_table}')
            structure = cur.fetchall()

        col_names = [r[0] for r in structure]
        col_types = {r[0]: r[1] for r in structure}
        type_map  = {n: mysql_type_to_sa(t) for n, t in col_types.items()}

        exclude   = {args.id_outlet_field, args.country_field, 'created_at', 'last_refresh'}
        data_cols = [n for n in col_names if n not in exclude]

        with closing(src_conn.cursor()) as cur:
            cur.execute(f'SELECT DISTINCT {args.country_field} FROM {args.orig_table}')
            countries = [r[0] for r in cur.fetchall() if r[0] and r[0].strip()]

        print(f"Found {len(countries)} countries: {countries}")

        if args.dry_run:
            print('[dry-run] Would create:')
            for c in countries:
                print(f'  database={args.id_platform}, table={c}')
            return

        with connect(target_cfg) as tgt_conn:
            with closing(tgt_conn.cursor()) as cur:
                cur.execute(
                    f'CREATE DATABASE IF NOT EXISTS {args.id_platform} CHARACTER SET utf8mb4;'
                )
            tgt_conn.commit()

            engine = create_engine(
                f"mysql+pymysql://{target_cfg['user']}:{target_cfg['passwd']}"
                f"@{target_cfg['host']}/{args.id_platform}",
                echo=False,
            )

            for country in countries:
                print(f'\n--- {country} ---')

                try:
                    Table(country, MetaData(), autoload_with=engine, schema=args.id_platform)
                    print(f'  Table {args.id_platform}.{country} already exists — skipping create.')
                except Exception:
                    cols = [
                        Column(args.id_outlet_field, String(50), primary_key=True),
                        *[Column(c, type_map[c]) for c in data_cols],
                        Column('created_at', DateTime,
                               server_default=text('CURRENT_TIMESTAMP')),
                        Column('last_refresh', DateTime,
                               server_default=text(
                                   'CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP')),
                    ]
                    Table(country, MetaData(), *cols, schema=args.id_platform).create(engine)
                    print(f'  Created table {args.id_platform}.{country}')

                ordered = [
                    c for c in col_names
                    if c not in {'created_at', 'last_refresh', args.country_field}
                ] + ['created_at', 'last_refresh']

                data_df = pd.read_sql(
                    f'SELECT {", ".join(ordered)} FROM {args.orig_table} '
                    f'WHERE {args.country_field} = %s',
                    src_conn,
                    params=(country,),
                )
                print(f'  {len(data_df)} rows to migrate')

                if data_df.empty:
                    continue

                insert_cols   = ', '.join(f'`{c}`' for c in data_df.columns)
                update_cols   = ', '.join(
                    f'`{c}` = VALUES(`{c}`)' for c in data_df.columns
                    if c != args.id_outlet_field
                )
                placeholders  = ', '.join(['%s'] * len(data_df.columns))
                upsert_sql    = (
                    f'INSERT INTO `{args.id_platform}`.`{country}` ({insert_cols}) '
                    f'VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_cols}'
                )

                with closing(tgt_conn.cursor()) as cur:
                    cur.executemany(upsert_sql, [tuple(r) for r in data_df.values])
                tgt_conn.commit()
                print(f'  Migrated successfully.')

    print(f'\nMigration complete for {args.id_platform}.')


if __name__ == '__main__':
    main()
