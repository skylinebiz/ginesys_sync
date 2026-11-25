"""
Stock Entry -> Ginesys Miscellaneous Entry.

On submit of a Stock Entry of one of DEFAULTS["stock_entry_types"], a background job posts
Miscellaneous Entries to the Ginesys WebAPI, the same REST API the Ginesys web UI uses:

    POST {web_api_url}token                  grant_type=password -> access_token
    POST {web_api_url}api/MiscEntry/Create   Bearer token, JSON body as the UI sends
    GET  {web_api_url}api/Session/Logout     Ginesys allows one session per user

Each row's Source Warehouse is an Issue and its Target Warehouse a Receive. A warehouse
points to the Ginesys stock point of the same name (its Warehouse Name), and one
Miscellaneous Entry is created per stock point. Stock point codes are read from Ginesys
(INVLOC) over the Oracle connection in Ginesys Settings.

A Ginesys failure never blocks the submit. Each stock point's result is added as a comment
on the Stock Entry; failures also go to Error Log, and resend() sends only what is missing.

The Stock Entry Types, warehouse overrides and Ginesys codes are in DEFAULTS below.
Per site, only the switch and the login go in site_config.json:

    "ginesys_misc_entry": {
        "enabled": 1,
        "username": "...",
        "password": "...",
        "kill_existing_session": 0   # 1: if this user is already logged in elsewhere, end that session
    }

Any other DEFAULTS key may also be set there to override it for one site. Environment
variables GINESYS_WEB_API_URL, GINESYS_WEB_USERNAME and GINESYS_WEB_PASSWORD are used when
the matching key is not in site_config.

Needs ec_production (required app) for the Stock Entry ec_lot field used in the remarks.
"""

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
import requests
from frappe import _
from frappe.utils import cint, flt, get_system_timezone, getdate

CONFIG_KEY = "ginesys_misc_entry"

RECEIVE = "Receive"
ISSUE = "Issue"

TIMEOUT = 60

# Ginesys allows one session per user, so every Ginesys call from any worker or site on
# this bench takes this Redis lock first (the key is not site-prefixed on purpose)
SESSION_LOCK = "ginesys_misc_entry:web_session"
SESSION_LOCK_TIMEOUT = 300  # lock expires if a worker dies holding it
SESSION_LOCK_WAIT = 600  # how long a job waits for the lock
# When the user is logged in elsewhere (e.g. a person in the Ginesys web UI)
LOGIN_RETRIES = 6
LOGIN_RETRY_WAIT = 10  # seconds

SUCCESS_PREFIX = "Ginesys Miscellaneous Entry created"
FAILURE_PREFIX = "Ginesys Miscellaneous Entry failed"
# Cancel flow, kept commented for later: Ginesys refuses quantity 0 ("zero quantity ...
# Entry not allowed"), so a cancelled Stock Entry would only mark its Ginesys entries
# (remarks get " / Cancelled", quantities stay). For now cancel is blocked instead, see
# block_cancel_if_sent.
# CANCELLED_PREFIX = "Ginesys Miscellaneous Entry marked Cancelled (Stock Entry cancelled):"

DEFAULTS = {
	"enabled": 0,
	"web_api_url": "http://192.168.3.3/WebAPI/",
	# Stock Entry Types that are sent to Ginesys (to be finalized). Direction comes from the
	# warehouses: Source Warehouse -> Issue, Target Warehouse -> Receive.
	"stock_entry_types": [
		# "Material Consumption for Manufacture",
		# "Material Transfer for Manufacture",
		"Disassemble",
	],
	# Warehouse -> Ginesys stock point name. By default the stock point name is the
	# Warehouse Name (without the company abbreviation), e.g. "Ambala Godown - G" -> "Ambala Godown".
	# Add an entry here only where the names differ, e.g. {"Stores - G": "POS(Udaipur)"}.
	"warehouse_stock_points": {},
	# Ginesys internal codes (ADMSITE.CODE etc.), read from Ginesys
	# "site_code": 1832,  # Site: Sakshi Jain POS
	# "site_code": 702,  # Site: Arihant Enterprises (AREN, OU 1)
	"site_code": 1,  # Site: Arihant Creations (ARCR, OU 1)
	"ou_code": 1,  # OU: Arihant
	"connected_site_code": 702,  # Arihant Enterprises (the user's connected site)
	"scheme_code": 323,  # Misc Entry No: (19) Misc Issue
}

ENV_VARS = {
	"web_api_url": "GINESYS_WEB_API_URL",
	"username": "GINESYS_WEB_USERNAME",
	"password": "GINESYS_WEB_PASSWORD",
}


class GinesysError(Exception):
	def __init__(self, message, response=None):
		super().__init__(message)
		self.response = response


def get_config():
	"""DEFAULTS, overridden by environment variables, overridden by site_config."""

	config = dict(DEFAULTS)

	for key, env_var in ENV_VARS.items():
		if os.environ.get(env_var):
			config[key] = os.environ[env_var]

	site_config = frappe.conf.get(CONFIG_KEY) or {}
	config.update({key: value for key, value in site_config.items() if value not in (None, "")})

	return config


# --------------------------------------------------------------------------------------
# Hook
# --------------------------------------------------------------------------------------


def is_synced_type(doc, config):
	return cint(config.get("enabled")) and doc.stock_entry_type in (config.get("stock_entry_types") or [])


def on_stock_entry_submit(doc, method=None):
	"""doc_events hook: queue the Ginesys Miscellaneous Entries for a Stock Entry."""

	if is_synced_type(doc, get_config()):
		enqueue("send_stock_entry", doc.name)


# def on_stock_entry_cancel(doc, method=None):
# 	"""doc_events hook: queue marking this Stock Entry's Ginesys entries as Cancelled."""
#
# 	if is_synced_type(doc, get_config()):
# 		enqueue("cancel_stock_entry", doc.name)


def enqueue(job, stock_entry):
	frappe.enqueue(
		f"ginesys_migration.integrations.ginesys_misc_entry.{job}",
		queue="short",
		at_front=True,  # priority: ahead of other jobs waiting in the short queue
		timeout=SESSION_LOCK_WAIT + SESSION_LOCK_TIMEOUT,  # may wait for the session lock
		stock_entry=stock_entry,
		enqueue_after_commit=True,
		job_id=f"ginesys_misc_entry::{job}::{stock_entry}",
		deduplicate=True,
	)


# --------------------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------------------


@contextmanager
def ginesys_session(config):
	"""
	A logged-in Ginesys client. Ginesys allows one session per user, so this holds a Redis
	lock shared by all workers and sites on this bench, and always logs out at the end.
	"""

	client = GinesysWebClient.from_config(config)
	lock = frappe.cache.lock(SESSION_LOCK, timeout=SESSION_LOCK_TIMEOUT, blocking_timeout=SESSION_LOCK_WAIT)
	if not lock.acquire():
		raise GinesysError(_("Timed out waiting for another Ginesys send to finish."))
	try:
		frappe.db.rollback()  # fresh reads: another job may have changed things while we waited
		yield client
	finally:
		try:
			client.logout()
		finally:
			lock.release()


def log_failure(stock_entry, what, error):
	"""Every sync failure goes to Error Log (first, so it is never lost) and to a comment."""

	response = error.response if isinstance(error, GinesysError) else None
	try:
		frappe.log_error(
			title=f"{FAILURE_PREFIX}{what}: {stock_entry}",
			message=frappe.get_traceback()
			+ (f"\n\nResponse:\n{json.dumps(response, indent=1, default=str)}" if response else ""),
			reference_doctype="Stock Entry",
			reference_name=stock_entry,
		)
	except Exception:
		frappe.log_error(title=f"{FAILURE_PREFIX}{what}: {stock_entry}")

	try:
		frappe.get_doc("Stock Entry", stock_entry).add_comment("Info", f"{FAILURE_PREFIX}{what}: {str(error)[:500]}")
	except Exception:
		frappe.log_error(title=f"{FAILURE_PREFIX}: could not add comment to {stock_entry}")


def get_comments(stock_entry, prefix):
	return frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "Stock Entry",
			"reference_name": stock_entry,
			"content": ["like", f"{prefix}%"],
		},
		pluck="content",
		order_by="creation asc",
	)


# Submit ---------------------------------------------------------------------------------


def success_comment_prefix(stock_point):
	return f"{SUCCESS_PREFIX} for stock point {stock_point}:"


def already_sent(stock_entry, stock_point):
	return bool(get_comments(stock_entry, success_comment_prefix(stock_point)))


def get_pending_groups(se, config):
	"""Stock point groups of this Stock Entry that are not in Ginesys yet."""

	return [group for group in group_by_stock_point(se, config) if not already_sent(se.name, group["stock_point"])]


def send_stock_entry(stock_entry):
	"""
	One Ginesys Miscellaneous Entry per stock point of the Stock Entry's warehouses.
	Each one is tracked by a comment, so a resend only sends the ones still missing.
	"""

	try:
		se = frappe.get_doc("Stock Entry", stock_entry)
		if se.docstatus != 1:
			return  # cancelled before this job ran
		config = get_config()

		if get_pending_groups(se, config):
			with ginesys_session(config) as client:
				stock_points = get_stock_points(config)
				for group in get_pending_groups(se, config):
					send_group(se, group, stock_points, config, client)

	except Exception as e:
		log_failure(stock_entry, "", e)

	frappe.db.commit()


def send_group(se, group, stock_points, config, client):
	"""Create the Ginesys entry for one stock point; a failure is recorded and the rest go on."""

	stock_point = group["stock_point"]
	try:
		ginesys_stock_point = stock_points.get(stock_point.casefold())
		if not ginesys_stock_point:
			raise GinesysError(
				_("Stock point {0} (warehouse {1}) does not exist in Ginesys for site code {2}.").format(
					stock_point, ", ".join(group["warehouses"]), config["site_code"]
				)
			)

		payload = build_payload(se, group, ginesys_stock_point, config)
		result = client.create_misc_entry(payload)

		misc_no = result.get("schemeDocNo") or result.get("schemeDocno") or ""
		se.add_comment(
			"Info",
			f"{success_comment_prefix(stock_point)} {misc_no} "
			f"(Issue {len(group['issue'])} item(s), Receive {len(group['receive'])} item(s), "
			f"Ginesys code {result.get('code')})",
		)

	except Exception as e:
		log_failure(se.name, f" for stock point {stock_point}", e)


@frappe.whitelist()
def resend(stock_entry):
	"""Send the stock points of a submitted Stock Entry that are not in Ginesys yet."""

	frappe.only_for("System Manager")

	se = frappe.get_doc("Stock Entry", stock_entry)
	if se.docstatus != 1:
		frappe.throw(_("Only submitted Stock Entries can be sent."))

	if not get_pending_groups(se, get_config()):
		frappe.throw(_("{0} was already sent to Ginesys.").format(stock_entry))

	enqueue("send_stock_entry", stock_entry)


# Cancel ---------------------------------------------------------------------------------


def get_sent_entries(stock_entry):
	"""[(ginesys_code, misc_no)] of the Ginesys entries created for this Stock Entry."""

	entries = []
	for content in get_comments(stock_entry, SUCCESS_PREFIX):
		code = re.search(r"Ginesys code (\d+)", content)
		misc_no = re.search(r"\b(IMS[\w-]+)", content)
		if code:
			entries.append((code.group(1), misc_no.group(1) if misc_no else ""))
	return entries


def block_cancel_if_sent(doc, method=None):
	"""doc_events before_cancel: a Stock Entry with entries in Ginesys cannot be cancelled."""

	misc_nos = [misc_no or code for code, misc_no in get_sent_entries(doc.name)]
	if misc_nos:
		frappe.throw(
			_(
				"Stock Entry {0} cannot be cancelled: Ginesys Miscellaneous Entries {1} were created for it. "
				"Cancelling in ERPNext does not reverse them in Ginesys."
			).format(frappe.bold(doc.name), ", ".join(frappe.bold(n) for n in misc_nos)),
			title=_("Sent to Ginesys"),
		)


# def already_marked(stock_entry, ginesys_code):
# 	return any(f"Ginesys code {ginesys_code})" in c for c in get_comments(stock_entry, CANCELLED_PREFIX))
#
#
# def cancel_stock_entry(stock_entry):
# 	"""Stock Entry cancelled: add " / Cancelled" to the remarks of its Ginesys entries."""
#
# 	try:
# 		pending = [(code, no) for code, no in get_sent_entries(stock_entry) if not already_marked(stock_entry, code)]
# 		if not pending:
# 			return
#
# 		with ginesys_session(get_config()) as client:
# 			for code, misc_no in pending:
# 				if already_marked(stock_entry, code):
# 					continue
# 				mark_entry_cancelled(stock_entry, code, misc_no, client)
#
# 	except Exception as e:
# 		log_failure(stock_entry, " (cancel)", e)
#
# 	frappe.db.commit()
#
#
# def mark_entry_cancelled(stock_entry, code, misc_no, client):
# 	try:
# 		entry = client.get_misc_entry(code)
# 		if not entry.get("canModify", True):
# 			raise GinesysError(_("Ginesys entry {0} can no longer be modified (authorized?).").format(misc_no or code), entry)
#
# 		client.update_misc_entry(build_cancelled_payload(entry))
#
# 		frappe.get_doc("Stock Entry", stock_entry).add_comment(
# 			"Info", f"{CANCELLED_PREFIX} {entry.get('schemeDocno') or misc_no} (Ginesys code {code})"
# 		)
#
# 	except Exception as e:
# 		log_failure(stock_entry, f" (cancel, Ginesys entry {misc_no or code})", e)
#
#
# @frappe.whitelist()
# def resend_cancel(stock_entry):
# 	"""Mark the Ginesys entries of a cancelled Stock Entry as Cancelled again, e.g. after a failure."""
#
# 	frappe.only_for("System Manager")
#
# 	if frappe.db.get_value("Stock Entry", stock_entry, "docstatus") != 2:
# 		frappe.throw(_("Only cancelled Stock Entries can be marked Cancelled in Ginesys."))
#
# 	enqueue("cancel_stock_entry", stock_entry)


# --------------------------------------------------------------------------------------
# Stock points
# --------------------------------------------------------------------------------------


def get_stock_point_name(warehouse, config):
	"""Ginesys stock point name for an ERPNext warehouse."""

	override = (config.get("warehouse_stock_points") or {}).get(warehouse)
	if override:
		return override
	return (frappe.db.get_value("Warehouse", warehouse, "warehouse_name") or warehouse).strip()


def get_stock_points(config):
	"""
	Active stock points of the Ginesys site, keyed by casefolded name:
	{"pos(udaipur)": {"code": 1189794, "name": "POS(Udaipur)", "loc_type": "W"}}.
	Read from Ginesys (INVLOC) over the Oracle connection in Ginesys Settings.
	"""

	from ginesys_migration.utils.oracle import get_ginesys_connection

	connection = get_ginesys_connection()
	try:
		cursor = connection.cursor()
		cursor.execute(
			"SELECT LOCCODE, LOCNAME, LOCTYPE FROM INVLOC WHERE ADMSITE_CODE = :site_code AND NVL(EXT, 'N') = 'N'",
			site_code=cint(config["site_code"]),
		)
		return {
			name.strip().casefold(): {"code": code, "name": name.strip(), "loc_type": loc_type}
			for code, name, loc_type in cursor.fetchall()
		}
	finally:
		connection.close()


# --------------------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------------------


def get_ginesys_item_id(item_code):
	"""Ginesys item id = the Item's first barcode (the Ginesys icode), else the item code."""

	barcode = frappe.db.get_value(
		"Item Barcode", {"parent": item_code, "parenttype": "Item"}, "barcode", order_by="idx asc"
	)
	return (barcode or item_code).strip()


def group_by_stock_point(se, config):
	"""
	Split the Stock Entry rows by stock point: a row's Source Warehouse is an Issue at that
	warehouse's stock point, its Target Warehouse a Receive there. A row with both (a
	transfer) gives an Issue at one stock point and a Receive at the other.

	Returns [{"stock_point", "warehouses", "issue": {item_id: qty}, "receive": {item_id: qty}}]
	in the order the stock points first appear.
	"""

	groups = {}

	def add(warehouse, direction, item_id, qty):
		stock_point = get_stock_point_name(warehouse, config)
		group = groups.setdefault(
			stock_point, {"stock_point": stock_point, "warehouses": [], "issue": {}, "receive": {}}
		)
		if warehouse not in group["warehouses"]:
			group["warehouses"].append(warehouse)
		lines = group["issue" if direction == ISSUE else "receive"]
		lines[item_id] = lines.get(item_id, 0) + qty

	for row in se.items:
		qty = flt(row.transfer_qty or row.qty)
		if not qty:
			continue
		item_id = get_ginesys_item_id(row.item_code)
		if row.s_warehouse:
			add(row.s_warehouse, ISSUE, item_id, qty)
		if row.t_warehouse:
			add(row.t_warehouse, RECEIVE, item_id, qty)

	return list(groups.values())


def build_line_items(quantities, direction):
	"""Receive quantities are negative, as the Ginesys UI sends them."""

	sign = -1 if direction == RECEIVE else 1

	return [
		{
			"code": 0,
			"rowIndex": index,
			"itemId": item_id,
			"rateCal": 0,
			"quantity": sign * qty,
			"dataVersion": 0,
			"batchSerialNo": "",
			"batchSerialCode": None,
			"batchMfgDate": None,
			"batchExpDate": None,
			"priceManagement": "I",
			"itemManagementMode": "I",
			"validityMode": "N",
			"validityPeriod": 0,
			"manageExpiry": "N",
			"posBatchSerialMode": "",
		}
		for index, (item_id, qty) in enumerate(quantities.items())
		if qty
	]


def get_misc_date(posting_date):
	"""Midnight of the posting date in the system timezone, as an ISO string with offset."""

	date = getdate(posting_date)
	midnight = datetime(date.year, date.month, date.day, tzinfo=ZoneInfo(get_system_timezone()))
	return midnight.isoformat()


def build_payload(stock_entry, group, ginesys_stock_point, config):
	"""MiscEntry/Create body for one stock point group (see group_by_stock_point)."""

	issue_lines = build_line_items(group["issue"], ISSUE)
	receive_lines = build_line_items(group["receive"], RECEIVE)
	if not issue_lines and not receive_lines:
		frappe.throw(_("Stock Entry {0} has no items to send").format(stock_entry.name))

	ou_code = cint(config["ou_code"])
	stock_point_code = str(ginesys_stock_point["code"])

	# Remarks: "<EC Lot> / <Stock Entry>", e.g. "TT-001 / MAT-STE-00004"
	remarks = " / ".join(filter(None, [stock_entry.get("ec_lot"), stock_entry.name]))

	# Same shape as the Ginesys Miscellaneous Entry screen's save (MiscEntry/Create)
	return {
		"code": 0,
		"hoStockPoint": {
			"locType": ginesys_stock_point["loc_type"],
			"name": ginesys_stock_point["name"],
			"code": stock_point_code,
			"locCode": stock_point_code,
		},
		"storeStockpoint": None,
		"year": {"code": 0},
		"connectedOUCode": ou_code,
		"ou": {"code": ou_code},
		"costCenter": {"code": 0},
		"docSchemeName": {"code": cint(config["scheme_code"])},
		"documentSite": {"code": cint(config["site_code"])},
		"connectedSiteCode": cint(config["connected_site_code"]),
		"miscNo": 0,
		"miscDate": get_misc_date(stock_entry.posting_date),
		"deptCode": 0,
		"documentNo": "",
		"remarks": remarks[:500],
		"gstApplicable": "N",
		"schemeDocno": "",
		"miscellaneousIssueLineItemDetails": issue_lines,
		"miscellaneousReceiveLineItemDetails": receive_lines,
		"deletedIssueLineItemDetails": [],
		"deletedReceiveLineItemDetails": [],
		"confirmAuthorization": False,
		"ignoreNegativeStockCheck": False,
		"ignoreConfirmation": False,
		"menuShortCode": "invmiscentry",
		"createdBy": None,
		"modifiedBy": None,
		"authorizedBy": None,
		"integrationMain": None,
		"udfDetails": [],
		"attachmentFolder": None,
		"attachedFiles": {"timestamp": 0, "docType": "", "docCode": 0, "files": []},
		"requestId": int(time.time() * 1000),
	}


# CANCELLED_REMARK = "Cancelled"
#
#
# def cancelled_remarks(remarks):
# 	"""'TT-001 / MAT-STE-00017' -> 'TT-001 / MAT-STE-00017 / Cancelled' (only once)."""
#
# 	remarks = (remarks or "").strip()
# 	if remarks.endswith(CANCELLED_REMARK):
# 		return remarks
# 	return " / ".join(filter(None, [remarks, CANCELLED_REMARK]))[:500]
#
#
# def build_cancelled_payload(entry):
# 	"""
# 	MiscEntry/Update body that keeps a Ginesys entry as it is, with " / Cancelled" added to
# 	the remarks. Quantities are sent back unchanged (Receive ones are already negative).
# 	`entry` is GetMiscellaneousEntry's result; the shape follows the Ginesys screen's save
# 	in edit mode (existing line codes, dataVersions and oldQty are sent back).
# 	"""
#
# 	batch_fields = (
# 		"batchSerialNo", "batchSerialCode", "batchMfgDate", "priceManagement", "itemManagementMode",
# 		"validityMode", "validityPeriod", "manageExpiry",
# 	)
#
# 	def line(row, index, issue):
# 		data = {
# 			"code": row.get("code") or 0,
# 			"rowIndex": index,
# 			"itemId": row.get("itemId"),
# 			"rateCal": row.get("rateCal") or 0,
# 			"quantity": row.get("quantity"),
# 			"dataVersion": row.get("dataVersion") or 0,
# 			"batchExpDate": row.get("batchValidTillDate"),
# 			"posBatchSerialMode": row.get("posBatchSerialMode") or "",
# 		}
# 		data.update({field: row.get(field) for field in batch_fields})
# 		if issue:
# 			data.update(
# 				{
# 					field: row.get(field)
# 					for field in (
# 						"requisitionDetCode", "requisitionMainCode", "requisitionNo", "requisitionDt",
# 						"oldQty", "refHdrDataVersion", "refDetDataVersion",
# 					)
# 				}
# 			)
# 		return data
#
# 	def code_of(key):
# 		return {"code": (entry.get(key) or {}).get("code") or 0}
#
# 	stock_point = entry.get("hoStockPoint") or {}
# 	ou_code = code_of("ou")["code"]
#
# 	return {
# 		"code": entry["code"],
# 		"dataVersion": entry.get("dataVersion"),
# 		"hoStockPoint": {
# 			"locType": stock_point.get("locType"),
# 			"name": stock_point.get("name"),
# 			"code": str(stock_point.get("code")),
# 			"locCode": str(stock_point.get("code")),
# 		}
# 		if stock_point
# 		else None,
# 		"storeStockpoint": entry.get("storeStockpoint"),
# 		"year": code_of("year"),
# 		"connectedOUCode": ou_code or None,
# 		"ou": code_of("ou"),
# 		"costCenter": code_of("costCenter"),
# 		"docSchemeName": code_of("docSchemeName"),
# 		"documentSite": code_of("documentSite"),
# 		"connectedSiteCode": cint(get_config()["connected_site_code"]),
# 		"miscNo": entry.get("miscNo") or 0,
# 		"miscDate": entry.get("miscDate"),
# 		"deptCode": entry.get("deptCode") or 0,
# 		"documentNo": entry.get("documentNo") or "",
# 		"remarks": cancelled_remarks(entry.get("remarks")),
# 		"gstApplicable": entry.get("gstApplicable") or "N",
# 		"schemeDocno": entry.get("schemeDocno") or "",
# 		"miscellaneousIssueLineItemDetails": [
# 			line(row, i, True) for i, row in enumerate(entry.get("miscellaneousIssueLineItemDetails") or [])
# 		],
# 		"miscellaneousReceiveLineItemDetails": [
# 			line(row, i, False) for i, row in enumerate(entry.get("miscellaneousReceiveLineItemDetails") or [])
# 		],
# 		"deletedIssueLineItemDetails": [],
# 		"deletedReceiveLineItemDetails": [],
# 		"confirmAuthorization": False,
# 		"ignoreNegativeStockCheck": False,
# 		"ignoreConfirmation": False,
# 		"menuShortCode": "invmiscentry",
# 		"udfDetails": [],
# 		"attachmentFolder": None,
# 		"attachedFiles": entry.get("attachedFiles") or {"timestamp": 0, "docType": "", "docCode": 0, "files": []},
# 		"requestId": int(time.time() * 1000),
# 	}
#
#


# --------------------------------------------------------------------------------------
# Ginesys WebAPI client
# --------------------------------------------------------------------------------------


class GinesysWebClient:
	def __init__(self, base_url, username, password, kill_existing_session=False):
		self.base_url = base_url.rstrip("/") + "/"
		self.username = username
		self.password = password
		self.kill_existing_session = kill_existing_session
		self.session = requests.Session()
		self.token = None
		self.user_id = None

	@classmethod
	def from_config(cls, config):
		username = (config.get("username") or "").strip()
		password = config.get("password") or ""
		if not username or not password:
			raise GinesysError(
				_("Set username and password under {0} in site_config.json, or GINESYS_WEB_USERNAME and GINESYS_WEB_PASSWORD.").format(
					CONFIG_KEY
				)
			)
		return cls(
			config["web_api_url"], username, password, cint(config.get("kill_existing_session"))
		)

	def login(self):
		"""
		Get a token. Ginesys allows one session per user; a second login is refused with
		"012|<message>|<userId>". Then wait and retry (the other session may log out),
		and if it is still there and kill_existing_session is on, end it and log in again,
		as the Ginesys login screen does when you answer Yes to "kill the session?".
		"""

		data = self._request_token()

		for _attempt in range(LOGIN_RETRIES):
			if data.get("access_token") or not self._already_logged_in(data):
				break
			time.sleep(LOGIN_RETRY_WAIT)
			data = self._request_token()

		if self._already_logged_in(data) and self.kill_existing_session:
			user_id = data["error_description"].split("|")[2]
			self.session.get(self.base_url + "api/Session/KillUser", params={"userId": user_id}, timeout=TIMEOUT)
			data = self._request_token()

		if not data.get("access_token"):
			message = data.get("error_description") or data.get("error") or str(data)[:300]
			raise GinesysError(_("Ginesys login failed: {0}").format(message), data)

		self.token = data["access_token"]
		self.user_id = data.get("userId")

	@staticmethod
	def _already_logged_in(data):
		error = (data.get("error_description") or "").split("|")
		return not data.get("access_token") and error[0] == "012" and len(error) > 2

	def _request_token(self):
		response = self.session.post(
			self.base_url + "token",
			data={"grant_type": "password", "username": self.username, "password": self.password},
			timeout=TIMEOUT,
		)
		data = self._json(response)
		if response.status_code != 200 and not data:
			data = {"error_description": response.text[:300]}
		return data

	def logout(self):
		"""End the session so the next login is not refused as 'already logged in'."""

		if not self.token:
			return
		try:
			self.session.get(
				self.base_url + "api/Session/Logout",
				params={"userId": self.user_id, "sessionId": None},
				headers={"Authorization": f"Bearer {self.token}"},
				timeout=TIMEOUT,
			)
		except requests.RequestException:
			pass
		self.token = None

	def post(self, action, payload):
		if not self.token:
			self.login()
		response = self.session.post(
			self.base_url + "api/" + action,
			json=payload,
			headers={"Authorization": f"Bearer {self.token}"},
			timeout=TIMEOUT,
		)
		data = self._json(response)
		if response.status_code != 200 or not data.get("success"):
			error = (data.get("error") or {}).get("message") or response.text[:300]
			raise GinesysError(_("Ginesys {0} failed: {1}").format(action, error), data)
		return data.get("result")

	def get_misc_entry(self, code):
		"""The entry as the Ginesys screen loads it for editing."""

		if not self.token:
			self.login()
		response = self.session.get(
			self.base_url + "api/MiscEntry/GetMiscellaneousEntry",
			params={"code": code},
			headers={"Authorization": f"Bearer {self.token}"},
			timeout=TIMEOUT,
		)
		data = self._json(response)
		if response.status_code != 200 or not data.get("success") or not data.get("result"):
			error = (data.get("error") or {}).get("message") or response.text[:300]
			raise GinesysError(_("Ginesys could not read entry {0}: {1}").format(code, error), data)
		return data["result"]

	def create_misc_entry(self, payload):
		return self._save("MiscEntry/Create", payload)

	def update_misc_entry(self, payload):
		return self._save("MiscEntry/Update", payload)

	def _save(self, action, payload):
		result = self.post(action, payload) or {}

		# HTTP success with result.success = false: negative stock or row errors
		if result.get("success") is False:
			reasons = [
				json.dumps(result.get("stockCheckResult"), default=str) if result.get("stockCheckResult") else None,
				json.dumps(result.get("errorRecords"), default=str) if result.get("errorRecords") else None,
				", ".join(result.get("errorMsg") or []),
			]
			reason = "; ".join(filter(None, reasons)) or "no reason given"
			raise GinesysError(_("Ginesys did not save the entry: {0}").format(reason), result)

		return result

	@staticmethod
	def _json(response):
		try:
			return response.json() or {}
		except ValueError:
			return {}
