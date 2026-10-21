// "Sync Ginesys Data" button on the Customer, Supplier, Item Group and Item lists.
// Opens the common sync dialog (ginesys_sync_dialog.js) with the last sync time and
// the number of records (Ginesys Sync Setting > Limit).

(function () {
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

			ginesys_migration.sync.resume_tracking();

			listview.page.add_inner_button(__("Sync Ginesys Data"), () =>
				ginesys_migration.sync.open_dialog(SYNC_TYPES[doctype])
			);
		};
	}
})();
