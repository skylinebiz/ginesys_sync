# Changelog

All notable changes to the Ginesys Migration app are documented in this file.

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
