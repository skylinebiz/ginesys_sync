(function () {
    function override() {
        const Scanner = erpnext?.utils?.BarcodeScanner;

        if (!Scanner || Scanner.__cleanup_override) return;
        Scanner.__cleanup_override = true;

        Scanner.prototype.clean_up = function () {
            // console.log('override cleanup');
            
            refresh_field(this.items_table_name);
        };

        // console.log("BarcodeScanner.clean_up overridden");
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