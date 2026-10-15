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

	// The button is added to the open list view from the router, not through
	// frappe.listview_settings: other apps (e.g. ec's Style Creator on Item) assign
	// the whole settings object, which would drop an onload added here.
	if (frappe.router && frappe.router.on) {
		frappe.router.on("change", () => add_button(0));
		add_button(0);
	}

	function add_button(attempt) {
		const route = frappe.get_route();

		if (route[0] !== "List" || !SYNC_TYPES[route[1]]) return;

		if (!frappe.user.has_role("System Manager")) return;

		const listview = window.cur_list;

		// The list view is created after the route changes
		if (!listview || listview.doctype !== route[1] || !listview.page) {
			if (attempt < 20) setTimeout(() => add_button(attempt + 1), 250);
			return;
		}

		if (listview.ginesys_sync_button) return;

		listview.ginesys_sync_button = true;

		const $button = listview.page.add_inner_button(__("Sync Ginesys Data"), () =>
			open_dialog(listview, SYNC_TYPES[route[1]])
		);

		// Keep it with the other action buttons, before the List View switcher
		const $switcher = listview.page.custom_actions && listview.page.custom_actions.find(".view-switcher");

		if ($button && $switcher && $switcher.length) {
			$button.insertBefore($switcher);
		}
	}

	function open_dialog(listview, sync_type) {
		frappe
			.call({ method: `${METHODS}.get_sync_info`, args: { sync_type } })
			.then((r) => show_dialog(listview, r.message));
	}

	function show_dialog(listview, info) {
		const sync_type = info.sync_type;
		let poll = null;
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
						watch();
						render(r.message || { state: "queued", sync_type });
					})
					.catch(() => dialog.enable_primary_action());
			},
		});

		dialog.onhide = () => {
			stop();
			frappe.realtime.off(SYNC_EVENT, on_event);
		};

		dialog.show();

		frappe.realtime.on(SYNC_EVENT, on_event);

		// A sync is already running (started here or from Ginesys Sync Setting)
		frappe.call({ method: `${METHODS}.get_sync_status` }).then((r) => {
			if (r.message && ACTIVE_STATES.includes(r.message.state)) {
				dialog.disable_primary_action();
				watch();
				render(r.message);
			}
		});

		function on_event(status) {
			if (status && status.sync_type === sync_type) render(status);
		}

		function watch() {
			watching = true;
			stop();

			poll = setInterval(() => {
				frappe.call({ method: `${METHODS}.get_sync_status` }).then((r) => {
					if (r.message && r.message.sync_type === sync_type) render(r.message);
				});
			}, POLL_MS);
		}

		function stop() {
			if (poll) {
				clearInterval(poll);
				poll = null;
			}
		}

		function finish() {
			watching = false;
			stop();
			dialog.set_primary_action(__("Close"), () => dialog.hide());
			dialog.enable_primary_action();
			listview.refresh();
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
})();
