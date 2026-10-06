# Changelog

All notable changes to the Ginesys Migration app are documented in this file.

## [1.3.0] - 2026-10-05

### Added

- **Ginesys Sync Setting** — new single doctype holding the Ginesys server connection (Server IP, Port, Username, Password), the Sync Type to run and the Limit (how many records to sync, default 1000). System Manager only; the password is stored encrypted.
- **Sync button** — on Ginesys Sync Setting, runs the selected Sync Type (Item, Item Group, Item Definition, Customer or Supplier) as a background job, syncing up to the Limit. A Limit above 1000 is synced 1000 records at a time with a 10 second wait after every 1000. Item Group ignores the Limit and syncs everything in a single run. A progress dialog shows the batch number and records done (it comes back if the page is reloaded mid-sync), and a "Success" message with the batch / synced / failed counts is shown on completion; a failure shows the error instead. Only one sync can run at a time.
- **Test Connection button** — checks that the saved settings can connect to Ginesys.
- **Customer Sync / Supplier Sync** — new optional `offset` argument (rows to skip, ordered by `SLCODE`), so these syncs can be run in batches. Without it they behave as before.

### Changed

- **All sync scripts** — the Ginesys connection now comes from Ginesys Sync Setting. Scripts can still be run from the console with `host`, `port` and `limit` arguments; `host` / `port` override the settings, and when neither is given the previous built-in defaults are used.
- **Sync Setting timestamps** — Item Group, Customer and Supplier syncs now record when they last completed in `last_item_group_sync`, `last_customer_sync` and `last_supplier_sync` (Item and Item Definition already saved theirs). `last_sync` is set whenever a sync started from the Sync button completes.
- **All sync scripts** — now return the run's `fetched` / `synced` / `failed` counts.

## [1.2.0] - 2026-09-30

### Added

- **Item Sync — non-finished items** — the item sync no longer filters `INVITEM` on `MATERIAL_TYPE = 'F'`; every material type is fetched and handled per row. Rows whose `MATERIAL_TYPE` is not `F` are synced as plain Items (no template / variant):
  - Item code is `ICODE`; rows without an ICODE are skipped.
  - Item name is `CNAME1`–`CNAME5` joined with spaces (empty ones skipped, cut to 140 characters), falling back to the ICODE. Description is left to ERPNext's default.
  - Price goes to a buying price list **Cost Price** (created if missing): MRP, or WSP when MRP is empty / 0. No MRP / WSP selling prices are created for these items.
  - HSN/SAC is taken from the Item Group; when the group has none, the Item gets `520829` (the GST HSN Code record is created if missing). Item Groups themselves are not changed.
  - Definitions: `DESC1`–`DESC6` → `custom_def_1`–`custom_def_6` as for finished items, plus `CNAME6` → `custom_def_7`. Barcodes (ICODE, BARCODE) are synced the same way as for finished items.

### Changed

- **Item Sync — finished items** (`MATERIAL_TYPE = 'F'`) — behaviour unchanged; the template / variant logic moved into its own `sync_finished_item` function. Item Groups without an HSN/SAC code are still reported under "Missing Item Groups" and those items skipped.
- **Item Sync** — the failed-item Error Log now also shows the ICODE and material type.

### Fixed

- **Item Sync** — a row whose Item Group doesn't exist in ERPNext crashed with an `AttributeError` before it could be recorded; it's now correctly counted and listed under "Missing Item Groups".

## [1.1.0] - 2026-09-29

### Added

- **Finished Item Sync** — now also fills the Item's "Definitions" section: Ginesys `DESC1`–`DESC6` are written to `custom_def_1`–`custom_def_6` in the same save as the description, vendor part number and barcodes.

### Changed

- **Customer Sync / Supplier Sync** — `customer_sync` and `supplier_sync` are now whitelisted, so they can be called from the desk / API like the other sync scripts.

### Fixed

- **Finished Item Sync / Item Definition Sync** — a `DESC` value longer than its Def field allows no longer fails the whole Item save with `CharacterLengthExceededError` (which also dropped the description, barcodes and prices). The too-long definition is left empty, the rest of the Item saves normally, and each run logs one "Item Definition Too Long" Error Log listing the item, field, length and full value. The limit is read from the field's own length, so it follows any change to the Def fields (e.g. EC 3.2.0's 1000 characters).

### Removed

- **Barcode scanner override** — `barcode_scanner_override.js` is no longer included in desk (`app_include_js`), so ERPNext's standard barcode scanning is used.

## [1.0.0] - 2026-09-28

### Added

- **Sync Setting** — single doctype holding the last-synced timestamp for each sync, so every run resumes where the previous one stopped.
- **Item Group Sync** — Ginesys `INVGRP` groups synced to ERPNext Item Groups (named `GRPNAME (GRPCODE)`) with HSN/SAC code.
- **Finished Item Sync** — Ginesys finished items (`INVITEM`, `MATERIAL_TYPE = 'F'`) synced as Item templates (Style No) with Colour / Colour Code / Size variants, including description, vendor part number, barcodes (moved from any other item that held them) and MRP / WSP prices.
- **Item Definition Sync** — `sync_item_definitions` copies Ginesys `DESC1`–`DESC6` into the matching Item variant's `custom_def_1`–`custom_def_6`, in batches (`limit`, default 500) resuming from `Sync Setting.last_item_desc_sync`. Items are matched on Style No + Colour + Colour Code + Size, falling back to the item code; unmatched items are logged.
- **Customer Sync** and **Supplier Sync** — Ginesys parties synced to ERPNext Customers and Suppliers, with GST state mapping.
- **EC** is now a required app (for the Item "Definitions" fields).
