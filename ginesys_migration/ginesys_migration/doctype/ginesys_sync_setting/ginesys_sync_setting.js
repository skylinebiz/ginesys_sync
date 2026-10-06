// Copyright (c) 2026, Harshit and contributors
// For license information, please see license.txt

const SYNC_EVENT = "ginesys_sync";
const SYNC_POLL_MS = 3000;
const SYNC_ACTIVE_STATES = ["queued", "running", "waiting"];
const SETTING_METHODS = "ginesys_migration.ginesys_migration.doctype.ginesys_sync_setting.ginesys_sync_setting";

frappe.ui.form.on("Ginesys Sync Setting", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => test_connection(frm));
		frm.add_custom_button(__("Sync"), () => start_sync(frm)).addClass("btn-primary");

		// Live updates from the background job; polling below covers a missed event
		frappe.realtime.off(SYNC_EVENT);
		frappe.realtime.on(SYNC_EVENT, (status) => render_sync_status(frm, status));

		// Pick the loader back up if a sync is already running (e.g. after a reload)
		frappe.call({ method: `${SETTING_METHODS}.get_sync_status` }).then((r) => {
			if (r.message && SYNC_ACTIVE_STATES.includes(r.message.state)) {
				watch_sync(frm);
				render_sync_status(frm, r.message);
			}
		});
	},
});

function ensure_saved(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Please save the settings first."));
		return false;
	}

	return true;
}

function test_connection(frm) {
	if (!ensure_saved(frm)) return;

	frappe.call({
		method: `${SETTING_METHODS}.test_connection`,
		freeze: true,
		freeze_message: __("Connecting to Ginesys..."),
	}).then((r) => {
		if (r.message) {
			frappe.msgprint({
				title: __("Success"),
				indicator: "green",
				message: __("Connected to Ginesys successfully."),
			});
		}
	});
}

function start_sync(frm) {
	if (!ensure_saved(frm)) return;

	const sync_type = frm.doc.sync_type;

	if (!sync_type) {
		frappe.msgprint(__("Please select a Sync Type."));
		return;
	}

	frappe.confirm(
		__("Sync up to {0} {1} records from Ginesys?", [frm.doc.sync_limit, __(sync_type).bold()]),
		() => {
			frappe.call({
				method: `${SETTING_METHODS}.start_sync`,
				args: { sync_type },
			}).then((r) => {
				watch_sync(frm);
				render_sync_status(frm, r.message || { state: "queued", sync_type });
			});
		}
	);
}

function watch_sync(frm) {
	frm.ginesys_sync_active = true;

	stop_sync_poll(frm);

	frm.ginesys_sync_poll = setInterval(() => {
		frappe.call({ method: `${SETTING_METHODS}.get_sync_status` }).then((r) => {
			render_sync_status(frm, r.message);
		});
	}, SYNC_POLL_MS);
}

function stop_sync_poll(frm) {
	if (frm.ginesys_sync_poll) {
		clearInterval(frm.ginesys_sync_poll);
		frm.ginesys_sync_poll = null;
	}
}

function render_sync_status(frm, status) {
	// Ignore late / duplicate updates once the sync has been reported as finished
	if (!frm.ginesys_sync_active || !status || !status.state) return;

	const sync_type = __(status.sync_type || "");
	const total = status.total || 0;
	const processed = Math.min(status.processed || 0, total);

	if (status.state === "completed") {
		finish_sync(frm);

		frappe.msgprint({
			title: __("Success"),
			indicator: "green",
			message: [
				__("{0} sync completed successfully.", [sync_type]),
				__("Batches: {0}", [status.batch || 0]),
				__("Synced: {0}", [status.synced || 0]),
				__("Failed: {0}", [status.failed || 0]),
			].join("<br>"),
		});

		return;
	}

	if (status.state === "failed") {
		finish_sync(frm);

		frappe.msgprint({
			title: __("{0} Sync Failed", [sync_type]),
			indicator: "red",
			message: [
				status.message || __("The sync failed. Check the Error Log."),
				__("Synced before failing: {0}", [status.synced || 0]),
			].join("<br>"),
		});

		return;
	}

	let description = __("Waiting for a background worker...");

	if (status.state === "running" && total) {
		description = __("Batch {0} of {1}: {2} of {3} records done", [
			status.batch,
			status.batches,
			processed,
			total,
		]);
	} else if (status.state === "running") {
		// Item Group syncs everything in one run, so there is no count up front
		description = __("Syncing...");
	} else if (status.state === "waiting") {
		description = __("Batch {0} of {1} done ({2} of {3}). Waiting {4} seconds before the next batch...", [
			status.batch,
			status.batches,
			processed,
			total,
			status.wait_seconds,
		]);
	}

	// total is 0 until the job has counted the pending records
	frappe.show_progress(
		__("Syncing {0} from Ginesys", [sync_type]),
		total ? processed : 0,
		total || 100,
		description
	);
}

function finish_sync(frm) {
	frm.ginesys_sync_active = false;

	stop_sync_poll(frm);
	frappe.hide_progress();
}
