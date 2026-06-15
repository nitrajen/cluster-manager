# Databricks notebook source
# node_metrics maintenance — run daily via scheduled Databricks Job
# Compacts small files, co-locates data for fast cluster/time queries,
# and enforces 7-day retention.

# COMMAND ----------

CATALOG = "main"
SCHEMA  = "cluster_manager"
TABLE   = "node_metrics"
RETENTION_DAYS = 7

full_table = f"`{CATALOG}`.`{SCHEMA}`.`{TABLE}`"

# COMMAND ----------
# MAGIC %md ### 1. Delete rows older than retention window

# COMMAND ----------

spark.sql(f"""
    DELETE FROM {full_table}
    WHERE ts < NOW() - INTERVAL {RETENTION_DAYS} DAYS
""")

# COMMAND ----------
# MAGIC %md ### 2. Compact small files and co-locate by cluster + time

# COMMAND ----------

spark.sql(f"""
    OPTIMIZE {full_table}
    ZORDER BY (cluster_id, ts)
""")

# COMMAND ----------
# MAGIC %md ### 3. Remove stale file versions

# COMMAND ----------

spark.sql(f"""
    VACUUM {full_table} RETAIN {RETENTION_DAYS * 24} HOURS
""")

# COMMAND ----------
# MAGIC %md ### 4. Quick sanity check

# COMMAND ----------

display(spark.sql(f"""
    SELECT
        COUNT(*)                            AS total_rows,
        COUNT(DISTINCT cluster_id)          AS clusters,
        MIN(ts)                             AS oldest,
        MAX(ts)                             AS newest,
        ROUND(MAX(ts) - MIN(ts))            AS span_days
    FROM {full_table}
"""))
