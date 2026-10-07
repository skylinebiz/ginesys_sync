# Copyright (c) 2026, Harshit and contributors
# For license information, please see license.txt

import time
from datetime import datetime

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from ginesys_migration.utils.oracle import get_ginesys_connection

SETTINGS_DOCTYPE = "Ginesys Sync Setting"

SYNC_JOB_ID = "ginesys_sync"
SYNC_EVENT = "ginesys_sync"
SYNC_STATUS_KEY = "ginesys_sync_status"
SYNC_TIMEOUT = 6 * 60 * 60

# Records synced per batch, and the pause after each batch before the next one
BATCH_SIZE = 1000
WAIT_SECONDS = 10

PARTY_COUNT_SQL = """
	SELECT COUNT(*)
	FROM FINSL f
	JOIN ADMCLS c
		ON f.CLSCODE = c.CLSCODE
	WHERE UPPER(c.CLSNAME) = '{cls}'
"""

# How each Sync Type option is run in batches:
#   timestamp - the script resumes from its Sync Setting timestamp (LAST_CHANGED)
#   offset    - the script has no resume point, so batches are paged with `offset`
#   once      - the script syncs everything in a single run
SYNC_TYPES = {
	"Item": {
		"method": "ginesys_migration.scripts.finished_item_sync.sync_finished_item_data",
		"mode": "timestamp",
		"timestamp_field": "last_item_sync",
		"count_sql": "SELECT COUNT(*) FROM INVITEM WHERE LAST_CHANGED >= :sync_from",
	},
	"Item Group": {
		"method": "ginesys_migration.scripts.sync_item_groups.sync_item_groups",
		"mode": "once",
	},
	"Item Definition": {
		"method": "ginesys_migration.scripts.item_definition_sync.sync_item_definitions",
		"mode": "timestamp",
		"timestamp_field": "last_item_desc_sync",
		"count_sql": (
			"SELECT COUNT(*) FROM INVITEM WHERE LAST_CHANGED >= :sync_from AND MATERIAL_TYPE = 'F'"
		),
	},
	"Customer": {
		"method": "ginesys_migration.scripts.customer_sync.customer_sync",
		"mode": "offset",
		"count_sql": PARTY_COUNT_SQL.format(cls="CUSTOMER"),
	},
	"Supplier": {
		"method": "ginesys_migration.scripts.supplier_sync.supplier_sync",
		"mode": "offset",
		"count_sql": PARTY_COUNT_SQL.format(cls="SUPPLIER"),
	},
}


class GinesysSyncSetting(Document):
	def validate(self):
		if cint(self.port) <= 0:
			frappe.throw(_("Port must be greater than 0."))

		if cint(self.sync_limit) <= 0:
			frappe.throw(_("Limit must be greater than 0."))


@frappe.whitelist()
def test_connection():
	"""Connect to Ginesys with the saved settings."""

	frappe.only_for("System Manager")

	conn = None
	cursor = None

	try:
		conn = get_ginesys_connection()
		cursor = conn.cursor()
		cursor.execute("SELECT 1 FROM DUAL")
		cursor.fetchone()

	except Exception as e:
		frappe.throw(
			_("Could not connect to Ginesys: {0}").format(str(e)),
			title=_("Connection Failed"),
		)

	finally:
		if cursor:
			cursor.close()

		if conn:
			conn.close()

	return True


@frappe.whitelist()
def start_sync(sync_type):
	"""Queue the selected sync as a background job."""

	frappe.only_for("System Manager")

	if sync_type not in SYNC_TYPES:
		frappe.throw(_("Please select a valid Sync Type."))

	if is_job_enqueued(SYNC_JOB_ID):
		frappe.throw(_("A Ginesys sync is already running."))

	set_sync_status(state="queued", sync_type=sync_type, user=frappe.session.user)

	frappe.enqueue(
		"ginesys_migration.ginesys_migration.doctype.ginesys_sync_setting.ginesys_sync_setting.run_sync",
		queue="long",
		timeout=SYNC_TIMEOUT,
		job_id=SYNC_JOB_ID,
		deduplicate=True,
		sync_type=sync_type,
		user=frappe.session.user,
		at_front=True
	)

	return get_sync_status()


@frappe.whitelist()
def get_sync_status():
	frappe.only_for("System Manager")

	status = frappe.cache.get_value(SYNC_STATUS_KEY) or {}

	# Job vanished without reporting (worker killed, timeout, ...)
	if status.get("state") in ("queued", "running", "waiting") and not is_job_enqueued(SYNC_JOB_ID):
		status = set_sync_status(
			**{
				**status,
				"state": "failed",
				"message": _("The sync stopped unexpectedly. Check the Error Log and RQ Job list."),
			}
		)

	return status


def set_sync_status(user=None, **status):
	"""Store the status for polling and push it to the user who started the sync."""

	status["user"] = user

	frappe.cache.set_value(SYNC_STATUS_KEY, status)

	frappe.publish_realtime(
		SYNC_EVENT,
		status,
		user=user,
	)

	return status


def run_sync(sync_type, user=None):
	"""
	Sync up to the Limit set in Ginesys Sync Setting. A Limit above BATCH_SIZE is
	synced BATCH_SIZE records at a time, pausing WAIT_SECONDS after each batch.
	"""

	config = SYNC_TYPES[sync_type]
	method = frappe.get_attr(config["method"])
	mode = config["mode"]

	settings = frappe.get_single(SETTINGS_DOCTYPE)

	limit = cint(settings.sync_limit) or BATCH_SIZE

	progress = {
		"sync_type": sync_type,
		"total": 0,
		"processed": 0,
		"synced": 0,
		"failed": 0,
		"batch": 0,
		"batches": -(-limit // BATCH_SIZE),
	}

	def add_result(result):
		result = result or {}
		fetched = cint(result.get("fetched"))

		progress["processed"] += fetched
		progress["synced"] += cint(result.get("synced"))
		progress["failed"] += cint(result.get("failed"))

		return fetched

	try:
		if mode == "once":
			progress["batch"] = progress["batches"] = 1
			set_sync_status(user=user, state="running", **progress)

			add_result(method())
			progress["total"] = progress["processed"]

		else:
			# Never more than Ginesys has pending
			limit = min(limit, count_pending(config))

			progress["total"] = limit
			progress["batches"] = -(-limit // BATCH_SIZE)

			while progress["processed"] < limit:
				progress["batch"] += 1

				batch_size = min(BATCH_SIZE, limit - progress["processed"])

				set_sync_status(user=user, state="running", **progress)

				if mode == "timestamp":
					sync_from = get_sync_timestamp(config)
					fetched = add_result(method(limit=batch_size))
				else:
					fetched = add_result(method(limit=batch_size, offset=progress["processed"]))

				# Limit reached, or a short batch: nothing left to sync
				if progress["processed"] >= limit or fetched < batch_size:
					break

				# The script resumes from the last row's LAST_CHANGED; if a full batch
				# did not move it, the next batch would fetch the same rows forever
				if mode == "timestamp" and get_sync_timestamp(config) == sync_from:
					frappe.throw(
						_(
							"More than {0} records share the same LAST_CHANGED ({1}) in Ginesys, "
							"so the sync cannot move past it. Run the sync from the console with a larger limit."
						).format(batch_size, sync_from)
					)

				# Don't hold a transaction open while sleeping
				frappe.db.commit()

				set_sync_status(user=user, state="waiting", wait_seconds=WAIT_SECONDS, **progress)
				time.sleep(WAIT_SECONDS)

		frappe.db.set_single_value("Sync Setting", "last_sync", now_datetime())
		frappe.db.commit()

		set_sync_status(user=user, state="completed", **progress)

	except Exception as e:
		frappe.db.rollback()

		frappe.log_error(
			title=f"Ginesys {sync_type} Sync (Batched) Failed",
			message=frappe.get_traceback(),
		)

		set_sync_status(user=user, state="failed", message=str(e), **progress)

		raise


def get_sync_timestamp(config):
	return frappe.db.get_single_value("Sync Setting", config["timestamp_field"], cache=False)


def count_pending(config):
	"""Number of Ginesys records the sync has to go through, for the progress bar."""

	params = {}

	if config["mode"] == "timestamp":
		last_sync = get_sync_timestamp(config)
		params["sync_from"] = get_datetime(last_sync) if last_sync else datetime(1970, 1, 1)

	conn = None
	cursor = None

	try:
		conn = get_ginesys_connection()
		cursor = conn.cursor()
		cursor.execute(config["count_sql"], params)

		return cint(cursor.fetchone()[0])

	finally:
		if cursor:
			cursor.close()

		if conn:
			conn.close()
