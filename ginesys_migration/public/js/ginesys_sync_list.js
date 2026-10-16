// "Sync Ginesys Data" button on the Customer, Supplier, Item Group and Item lists.
// Opens a dialog with the last sync time and the number of records (Ginesys Sync
// Setting > Limit), and runs the same background sync as the Sync button there.

(function () {
	const METHODS = "ginesys_migration.ginesys_migration.doctype.ginesys_sync_setting.ginesys_sync_setting";
	const SYNC_EVENT = "ginesys_sync";
	const POLL_MS = 3000;
	const ACTIVE_STATES = ["queued", "running", "waiting"];

	// List view doctype -> Sync Type in Ginesys Sync Setting
	const SYNC_TYPES = {
		Customer: "Customer",
		Supplier: "Supplier",
		"Item Group": "Item Group",
		Item: "Item",
	};

	Object.keys(SYNC_TYPES).forEach(register);

	// Standard list view hook: frappe.listview_settings[doctype].onload, loaded
	// through doctype_list_js in hooks.py. The onload another app may already have
	// set is called first, so both buttons are added.
	function register(doctype) {
		frappe.listview_settings = frappe.listview_settings || {};

		const settings = (frappe.listview_settings[doctype] = frappe.listview_settings[doctype] || {});

		// This file is loaded with every one of these lists, so only wrap once
		if (settings.ginesys_sync_registered) return;
		settings.ginesys_sync_registered = true;

		const onload = settings.onload;

		settings.onload = function (listview) {
			if (onload) onload.call(this, listview);

			if (!frappe.user.has_role("System Manager")) return;

			resume_tracking();

			listview.page.add_inner_button(__("Sync Ginesys Data"), () =>
				open_dialog(listview, SYNC_TYPES[doctype])
			);
		};
	}

	// The sync runs in a background job, so it is tracked here at page level, not by
	// the dialog: closing the dialog does not stop it, and the result is still
	// announced with an alert when it finishes.
	//
	// The file is loaded twice (app_include_js on every page, doctype_list_js with the
	// lists), so the state is shared on frappe to keep a single tracker.
	//   poll      - interval id while a sync is being tracked
	//   listening - the realtime listener is registered
	//   announced - the last tracked sync's result has already been shown
	//   open_view - { sync_type, render } of the dialog that is open, if any
	const state = (frappe.ginesys_sync_state = frappe.ginesys_sync_state || {
		poll: null,
		listening: false,
		announced: true,
		open_view: null,
		initialised: false,
	});

	function listen() {
		if (state.listening) return;

		try {
			frappe.realtime.on(SYNC_EVENT, handle_status);
			state.listening = true;
		} catch (e) {
			// Realtime unavailable; polling still reports the result
		}
	}

	function track() {
		listen();

		if (state.poll) return;

		state.announced = false;

		state.poll = setInterval(() => {
			frappe.call({ method: `${METHODS}.get_sync_status` }).then((r) => handle_status(r.message));
		}, POLL_MS);
	}

	function stop_tracking() {
		if (state.poll) {
			clearInterval(state.poll);
			state.poll = null;
		}
	}

	// A sync started earlier (here, or in Ginesys Sync Setting) is still running
	function resume_tracking() {
		if (state.poll) return;

		frappe.call({ method: `${METHODS}.get_sync_status` }).then((r) => {
			if (r.message && ACTIVE_STATES.includes(r.message.state)) track();
		});
	}

	function handle_status(status) {
		if (!status || !status.state) return;

		if (state.open_view && state.open_view.sync_type === status.sync_type) state.open_view.render(status);

		if (status.state !== "completed" && status.state !== "failed") return;

		stop_tracking();

		if (state.announced) return;
		state.announced = true;

		// Show the result in the list too
		if (window.cur_list && window.cur_list.doctype === status.sync_type) window.cur_list.refresh();

		// The open dialog already shows the result
		if (state.open_view) return;

		const sync_type = __(status.sync_type || "");

		if (status.state === "completed") {
			frappe.show_alert(
				{
					message: __("Ginesys {0} sync completed. Synced: {1}, Failed: {2}", [
						sync_type,
						status.synced || 0,
						status.failed || 0,
					]),
					indicator: "green",
				},
				15
			);
		} else {
			frappe.show_alert(
				{
					message: __("Ginesys {0} sync failed: {1}", [
						sync_type,
						frappe.utils.escape_html(status.message || __("Check the Error Log.")),
					]),
					indicator: "red",
				},
				20
			);
		}
	}

	function open_dialog(listview, sync_type) {
		frappe
			.call({ method: `${METHODS}.get_sync_info`, args: { sync_type } })
			.then((r) => show_dialog(listview, r.message));
	}

	function show_dialog(listview, info) {
		const sync_type = info.sync_type;
		let watching = false;

		const dialog = new frappe.ui.Dialog({
			title: __("Sync {0} from Ginesys", [__(sync_type)]),
			fields: [
				{
					fieldtype: "Data",
					fieldname: "last_sync",
					label: __("Last Sync"),
					read_only: 1,
					default: last_sync_text(info),
				},
				{
					fieldtype: "Data",
					fieldname: "records",
					label: __("Records"),
					read_only: 1,
					default: records_text(info),
					description: __("Set in Ginesys Sync Setting > Limit."),
				},
				{ fieldtype: "HTML", fieldname: "progress" },
			],
			primary_action_label: __("Start Sync"),
			primary_action() {
				dialog.disable_primary_action();

				frappe
					.call({ method: `${METHODS}.start_sync`, args: { sync_type } })
					.then((r) => {
						watching = true;
						track();
						render(r.message || { state: "queued", sync_type });
					})
					.catch(() => dialog.enable_primary_action());
			},
		});

		// Closing the dialog leaves the sync running; handle_status() alerts when it ends
		dialog.onhide = () => {
			if (state.open_view && state.open_view.render === render) state.open_view = null;
		};

		state.open_view = { sync_type, render };
		dialog.show();

		// A sync is already running (started here or from Ginesys Sync Setting)
		frappe.call({ method: `${METHODS}.get_sync_status` }).then((r) => {
			if (r.message && ACTIVE_STATES.includes(r.message.state)) {
				dialog.disable_primary_action();
				watching = true;
				track();
				render(r.message);
			}
		});

		function finish() {
			watching = false;
			dialog.set_primary_action(__("Close"), () => dialog.hide());
			dialog.enable_primary_action();
		}

		function render(status) {
			if (!watching || !status || !status.state) return;

			const total = status.total || 0;
			const processed = Math.min(status.processed || 0, total);
			const summary = [
				__("Batches: {0}", [status.batch || 0]),
				__("Synced: {0}", [status.synced || 0]),
				__("Failed: {0}", [status.failed || 0]),
			].join(" &middot; ");

			if (status.state === "completed") {
				finish();
				set_progress(100, "green", `<b>${__("Sync completed")}</b><br>${summary}`);
				return;
			}

			if (status.state === "failed") {
				finish();
				set_progress(
					100,
					"red",
					`<b>${__("Sync failed")}</b><br>${frappe.utils.escape_html(
						status.message || __("Check the Error Log.")
					)}<br>${summary}`
				);
				return;
			}

			let text = __("Waiting for a background worker...");

			if (status.state === "running" && total) {
				text = __("Batch {0} of {1}: {2} of {3} records done", [
					status.batch,
					status.batches,
					processed,
					total,
				]);
			} else if (status.state === "running") {
				// Item Group syncs everything in one run, so there is no count up front
				text = __("Syncing...");
			} else if (status.state === "waiting") {
				text = __("Batch {0} of {1} done ({2} of {3}). Waiting {4} seconds before the next batch...", [
					status.batch,
					status.batches,
					processed,
					total,
					status.wait_seconds,
				]);
			}

			set_progress(total ? Math.round((processed / total) * 100) : 5, "blue", text);
		}

		function set_progress(percent, colour, html) {
			const colours = { blue: "var(--primary)", green: "var(--green-500)", red: "var(--red-500)" };

			dialog.fields_dict.progress.$wrapper.html(`
				<div class="progress" style="height: 8px; margin: 12px 0 8px;">
					<div class="progress-bar" style="width: ${percent}%; background-color: ${colours[colour]};"></div>
				</div>
				<div class="text-muted small">${html}</div>
			`);
		}
	}

	function last_sync_text(info) {
		if (info.last_sync) {
			return frappe.datetime.str_to_user(info.last_sync);
		}

		// Timestamp syncs start from the default date when they have never run
		if (info.mode === "timestamp") {
			return __("Never synced - starts from {0}", [frappe.datetime.str_to_user(info.default_from)]);
		}

		return __("Never synced");
	}

	function records_text(info) {
		// Item Group syncs everything in one run and ignores the Limit
		if (info.mode === "once") return __("All records");

		return cstr(info.limit);
	}

	// On every desk page: pick up a sync that is already running and alert when it ends,
	// wherever the user is when it finishes
	if (!state.initialised) {
		state.initialised = true;

		$(document).on("startup", () => {
			if (frappe.user.has_role("System Manager")) resume_tracking();
		});
	}
})();
