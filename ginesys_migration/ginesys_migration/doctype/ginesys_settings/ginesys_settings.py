# Copyright (c) 2026, Harshit and contributors
# For license information, please see license.txt

import time
from datetime import datetime

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, get_time, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from ginesys_migration.utils.oracle import get_ginesys_connection

SETTINGS_DOCTYPE = "Ginesys Settings"
# Holds when each sync last ran (renamed from Sync Setting)
TRACKER_DOCTYPE = "Ginesys Sync Tracker"

SYNC_JOB_ID = "ginesys_sync"
SYNC_EVENT = "ginesys_sync"
SYNC_STATUS_KEY = "ginesys_sync_status"
SYNC_TIMEOUT = 6 * 60 * 60

# Records synced per batch, and the pause after each batch before the next one
BATCH_SIZE = 1000
WAIT_SECONDS = 10

# The daily scheduled sync, run in this order (Items need their Item Groups)
SCHEDULED_SYNC_TYPES = ("Item Group", "Item")
# Run by the Scheduled Job Type that update_sync_schedule keeps at the Sync Time
SCHEDULED_SYNC_METHOD = (
	"ginesys_migration.ginesys_migration.doctype.ginesys_settings.ginesys_settings.queue_scheduled_sync"
)

# Where a timestamp sync starts when it has never run (same default the scripts use)
DEFAULT_SYNC_FROM = datetime(1970, 1, 1)

PARTY_COUNT_SQL = """
	SELECT COUNT(*)
	FROM FINSL f
	JOIN ADMCLS c
		ON f.CLSCODE = c.CLSCODE
	WHERE UPPER(c.CLSNAME) = '{cls}'
"""

# How each Sync Type option is run in batches:
#   timestamp - the script resumes from its Ginesys Sync Tracker timestamp (LAST_CHANGED)
#   offset    - the script has no resume point, so batches are paged with `offset`
#   once      - the script syncs everything in a single run
# last_sync_field is the Ginesys Sync Tracker field the script saves when it has synced.
SYNC_TYPES = {
	"Item": {
		"method": "ginesys_migration.scripts.finished_item_sync.sync_finished_item_data",
		"mode": "timestamp",
		"timestamp_field": "last_item_sync",
		"last_sync_field": "last_item_sync",
		"count_sql": "SELECT COUNT(*) FROM INVITEM WHERE LAST_CHANGED >= :sync_from",
	},
	"Item Group": {
		"method": "ginesys_migration.scripts.sync_item_groups.sync_item_groups",
		"mode": "once",
		"last_sync_field": "last_item_group_sync",
	},
	"Customer": {
		"method": "ginesys_migration.scripts.customer_sync.customer_sync",
		"mode": "offset",
		"last_sync_field": "last_customer_sync",
		"count_sql": PARTY_COUNT_SQL.format(cls="CUSTOMER"),
	},
	"Supplier": {
		"method": "ginesys_migration.scripts.supplier_sync.supplier_sync",
		"mode": "offset",
		"last_sync_field": "last_supplier_sync",
		"count_sql": PARTY_COUNT_SQL.format(cls="SUPPLIER"),
	},
}


class GinesysSettings(Document):
	def validate(self):
		if cint(self.port) <= 0:
			frappe.throw(_("Port must be greater than 0."))

		if cint(self.sync_limit) <= 0:
			frappe.throw(_("Limit must be greater than 0."))

	def on_update(self):
		update_sync_schedule(self)


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

	if is_job_enqueued(SYNC_JOB_ID) or is_scheduled_sync_queued():
		frappe.throw(_("A Ginesys sync is already queued or running."))

	set_sync_status(state="queued", sync_type=sync_type, user=frappe.session.user)

	frappe.enqueue(
		"ginesys_migration.ginesys_migration.doctype.ginesys_settings.ginesys_settings.run_sync",
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
def get_sync_info(sync_type):
	"""What the "Sync Ginesys Data" dialog on the list views shows before a sync starts."""

	frappe.only_for("System Manager")

	if sync_type not in SYNC_TYPES:
		frappe.throw(_("Please select a valid Sync Type."))

	config = SYNC_TYPES[sync_type]

	return {
		"sync_type": sync_type,
		"mode": config["mode"],
		"last_sync": frappe.db.get_single_value(TRACKER_DOCTYPE, config["last_sync_field"], cache=False),
		"default_from": str(DEFAULT_SYNC_FROM),
		"limit": cint(frappe.db.get_single_value(SETTINGS_DOCTYPE, "sync_limit", cache=False)) or BATCH_SIZE,
	}


@frappe.whitelist()
def get_sync_status():
	frappe.only_for("System Manager")

	status = frappe.cache.get_value(SYNC_STATUS_KEY) or {}
	active = status.get("state") in ("queued", "running", "waiting")

	# The scheduled sync's trigger is still waiting on the default queue: report it as
	# queued, so the Sync buttons wait for it instead of offering to start another sync
	if not active and is_scheduled_sync_queued():
		return {"state": "queued", "sync_type": SCHEDULED_SYNC_TYPES[0]}

	# Job vanished without reporting (worker killed, timeout, ...)
	if active and not is_job_enqueued(SYNC_JOB_ID) and not is_scheduled_sync_queued():
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

	# A scheduled sync has no user to push to (publishing without one would reach
	# every user); it is still picked up by polling get_sync_status
	if user:
		frappe.publish_realtime(
			SYNC_EVENT,
			status,
			user=user,
		)

	return status


def run_sync(sync_type, user=None):
	"""
	Sync up to the Limit set in Ginesys Settings. A Limit above BATCH_SIZE is
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

		frappe.db.set_single_value(TRACKER_DOCTYPE, "last_sync", now_datetime())
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


def update_sync_schedule(settings):
	"""
	Keep the Scheduled Job Type that runs the daily sync at the Sync Time.

	The job always exists; it is stopped while Sync Time is empty. It is not declared in
	hooks.scheduler_events (its time is set at runtime), so it is linked to a Scheduler
	Event - `bench migrate` deletes every Scheduled Job Type that has neither.
	"""

	values = {
		"frequency": "Cron",
		# A cron is required even while stopped; it is never evaluated then
		"cron_format": get_sync_cron(settings.sync_time) or "0 0 * * *",
		"stopped": 0 if settings.sync_time else 1,
		"create_log": 1,
		"scheduler_event": get_scheduler_event(),
	}

	job_name = frappe.db.exists("Scheduled Job Type", {"method": SCHEDULED_SYNC_METHOD})

	if job_name:
		job = frappe.get_doc("Scheduled Job Type", job_name)

		if any(job.get(field) != value for field, value in values.items()):
			job.update(values)
			job.save(ignore_permissions=True)
	else:
		frappe.get_doc(
			{"doctype": "Scheduled Job Type", "method": SCHEDULED_SYNC_METHOD, **values}
		).insert(ignore_permissions=True)


def get_scheduler_event():
	"""The Scheduler Event that keeps the scheduled sync's job through migrations."""

	filters = {"scheduled_against": SETTINGS_DOCTYPE, "method": SCHEDULED_SYNC_METHOD}

	return (
		frappe.db.exists("Scheduler Event", filters)
		or frappe.get_doc({"doctype": "Scheduler Event", **filters}).insert(ignore_permissions=True).name
	)


def get_sync_cron(sync_time):
	"""Daily cron for a Sync Time, e.g. 02:30:00 -> "30 2 * * *"."""

	if not sync_time:
		return None

	sync_time = get_time(sync_time)

	return f"{sync_time.minute} {sync_time.hour} * * *"


def queue_scheduled_sync():
	"""
	Run by the scheduled sync's Scheduled Job Type at the Sync Time. Frappe always runs
	Cron jobs on the default queue with a short timeout (there is no long Cron frequency),
	so this only queues the sync itself on the long queue, at the front, the same way
	as the Sync button.
	"""

	# One sync at a time: skip today's run rather than queue it behind another sync.
	# (Not is_scheduled_sync_queued(): that is this job itself.)
	if is_job_enqueued(SYNC_JOB_ID):
		frappe.log_error(
			title="Ginesys Scheduled Sync Skipped",
			message=_("Another Ginesys sync was running at the Sync Time, so today's scheduled sync was skipped."),
		)
		return

	set_sync_status(state="queued", sync_type=SCHEDULED_SYNC_TYPES[0])

	frappe.enqueue(
		"ginesys_migration.ginesys_migration.doctype.ginesys_settings.ginesys_settings.run_scheduled_sync",
		queue="long",
		timeout=SYNC_TIMEOUT,
		job_id=SYNC_JOB_ID,
		deduplicate=True,
		enqueue_after_commit=True,
		at_front=True
	)


def is_scheduled_sync_queued():
	"""The scheduled sync's trigger (queue_scheduled_sync) is queued or running."""

	job_name = frappe.db.exists("Scheduled Job Type", {"method": SCHEDULED_SYNC_METHOD})

	return bool(job_name) and frappe.get_doc("Scheduled Job Type", job_name).is_job_in_queue()


def run_scheduled_sync():
	"""
	The daily scheduled sync: Item Groups, then Items, up to the Limit like the
	Sync button. A failed Item Group sync stops it, as the Items would need those groups.

	Each sync updates its own Ginesys Sync Tracker times as usual (Last Item Group Sync,
	Last Item Sync, Last Sync); Last Scheduled Sync is set once both have completed.
	"""

	for sync_type in SCHEDULED_SYNC_TYPES:
		run_sync(sync_type)

	frappe.db.set_single_value(TRACKER_DOCTYPE, "last_scheduled_sync", now_datetime())
	frappe.db.commit()


def get_sync_timestamp(config):
	return frappe.db.get_single_value(TRACKER_DOCTYPE, config["timestamp_field"], cache=False)


def count_pending(config):
	"""Number of Ginesys records the sync has to go through, for the progress bar."""

	params = {}

	if config["mode"] == "timestamp":
		last_sync = get_sync_timestamp(config)
		params["sync_from"] = get_datetime(last_sync) if last_sync else DEFAULT_SYNC_FROM

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
