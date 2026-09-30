(function () {
    function override() {
        const Scanner = erpnext?.utils?.BarcodeScanner;

        if (!Scanner || Scanner.__cleanup_override) return;
        Scanner.__cleanup_override = true;

        const original_process_scan = Scanner.prototype.process_scan;

        Scanner.prototype.process_scan = async function (...args) {
            this.__from_process_scan = true;

            try {
                return await original_process_scan.apply(this, args);
            } finally {
                this.__from_process_scan = false;
            }
        };

        Scanner.prototype.clean_up = function () {
            // Ignore cleanup unless called from process_scan
            // console.log('custom cleanup');
            
            if (!this.__from_process_scan) {
                return;
            }

            refresh_field(this.items_table_name);
        };
    }

    function wait() {
        if (erpnext?.utils?.BarcodeScanner) {
            override();
        } else {
            setTimeout(wait, 200);
        }
    }

    wait();
})();