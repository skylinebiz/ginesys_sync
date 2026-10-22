// Copyright (c) 2026, Harshit and contributors
// For license information, please see license.txt

const SETTING_METHODS = "ginesys_migration.ginesys_migration.doctype.ginesys_sync_setting.ginesys_sync_setting";

frappe.ui.form.on("Ginesys Sync Setting", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => test_connection(frm));
		frm.add_custom_button(__("Sync"), () => start_sync(frm)).addClass("btn-primary");
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

// Same dialog as the list views' "Sync Ginesys Data" (public/js/ginesys_sync_dialog.js),
// without the Last Sync / Records fields: after a confirmation it syncs the saved Sync Type
function start_sync(frm) {
	if (!ensure_saved(frm)) return;

	if (!frm.doc.sync_type) {
		frappe.msgprint(__("Please select a Sync Type."));
		return;
	}

	const sync_type = frm.doc.sync_type;

	// ginesys_migration.sync comes from the doctype_js hook
	const open_dialog = () => ginesys_migration.sync.open_dialog(sync_type, { show_info: false });

	// A sync is already running in the background: show its progress, nothing to confirm
	frappe.call({ method: `${SETTING_METHODS}.get_sync_status` }).then((r) => {
		if (r.message && ["queued", "running", "waiting"].includes(r.message.state)) {
			open_dialog();
			return;
		}

		frappe.confirm(
			__("Are you sure you want to sync {0} with a limit of {1}?", [
				frappe.utils.escape_html(__(sync_type)).bold(),
				frm.doc.sync_limit,
			]),
			open_dialog,
			null,
			__("Sync"),
			__("Cancel")
		);
	});
}
