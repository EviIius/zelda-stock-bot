"""Reliability tests for delivery ordering, retries, and state semantics."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import config
import detect
import notify
import product_catalog
import stock_monitor
import watchdog
from detect import Result, Status


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = config.STATE_FILE
        self.old_outbox = config.OUTBOX_FILE
        self.old_log_enabled = config.LOG_ENABLED
        self.old_runtime_heartbeat = config.RUNTIME_HEARTBEAT_FILE
        self.old_watchdog_state = config.WATCHDOG_STATE_FILE
        config.STATE_FILE = os.path.join(self.tmp.name, "state.json")
        config.OUTBOX_FILE = os.path.join(self.tmp.name, "outbox.json")
        config.LOG_ENABLED = False
        config.RUNTIME_HEARTBEAT_FILE = os.path.join(self.tmp.name, "runtime.json")
        config.WATCHDOG_STATE_FILE = os.path.join(self.tmp.name, "watchdog.json")

    def tearDown(self):
        config.STATE_FILE = self.old_state
        config.OUTBOX_FILE = self.old_outbox
        config.LOG_ENABLED = self.old_log_enabled
        config.RUNTIME_HEARTBEAT_FILE = self.old_runtime_heartbeat
        config.WATCHDOG_STATE_FILE = self.old_watchdog_state
        self.tmp.cleanup()

    def test_failed_delivery_is_queued_then_recorded_on_retry(self):
        calls = []

        def fail(_alert):
            calls.append("fail")
            raise RuntimeError("temporary failure")

        def succeed(_alert):
            calls.append("ok")

        with mock.patch.object(notify, "configured_channels", return_value=["discord"]), \
             mock.patch.dict(notify.CHANNELS, {"discord": fail}):
            results, delivered = notify.send_reliable(
                notify.Alert("stock", "body"), "stock:target:1",
                {"event_type": "stock", "product_key": "target", "repeat": 1},
            )
        self.assertFalse(delivered)
        self.assertIn("RuntimeError", results["discord"])
        self.assertTrue(notify.has_pending("stock:target:1"))

        items = notify._load_outbox()
        items[0]["next_retry_ts"] = 0
        notify._save_outbox(items)
        with mock.patch.dict(notify.CHANNELS, {"discord": succeed}):
            events = notify.retry_outbox()
        self.assertEqual(events[0]["product_key"], "target")
        self.assertFalse(notify.has_pending("stock:target:1"))
        self.assertEqual(calls, ["fail", "ok"])

    def test_alert_is_sent_before_confirmation(self):
        order = []
        state = {"version": 2, "products": {}}
        product = {"key": "target", "name": "Target", "url": "https://example.test",
                   "kind": "product", "enabled": True, "interval": 25, "cart_url": None}
        result = Result(Status.PREORDER, "enabled preorder", "browser")

        def send_first(*_args, **_kwargs):
            order.append("send")
            return {"discord": "ok"}, True

        def confirm(*_args, **_kwargs):
            order.append("confirm")
            return True

        with mock.patch.object(notify, "send_reliable", side_effect=send_first), \
             mock.patch.object(stock_monitor, "confirmed", side_effect=confirm), \
             mock.patch.object(stock_monitor, "_confirmation_notice"):
            stock_monitor.process_result(product, state, result, 0.2)

        self.assertEqual(order, ["send", "confirm"])
        entry = state["products"]["target"]
        self.assertGreater(entry["last_alert_ts"], 0)
        self.assertEqual(entry["alert_count"], 1)

    def test_failed_alert_does_not_advance_delivery_counters(self):
        state = {"version": 2, "products": {}}
        product = {"key": "target", "name": "Target", "url": "https://example.test",
                   "kind": "product", "enabled": True, "interval": 25, "cart_url": None}
        result = Result(Status.IN_STOCK, "enabled cart", "browser")

        with mock.patch.object(notify, "send_reliable", return_value=({"discord": "failed"}, False)), \
             mock.patch.object(notify, "has_pending", return_value=False), \
             mock.patch.object(stock_monitor, "confirmed", return_value=True):
            stock_monitor.process_result(product, state, result, 0.2)

        entry = state["products"]["target"]
        self.assertEqual(entry["last_alert_ts"], 0)
        self.assertEqual(entry["alert_count"], 0)
        self.assertTrue(entry["pending_alert_id"].startswith("stock:target:"))

    def test_confirmation_disabled_does_not_claim_second_check(self):
        state = {"version": 2, "products": {}}
        product = {"key": "target", "name": "Target", "url": "https://example.test",
                   "kind": "product", "enabled": True, "interval": 25, "cart_url": None}
        result = Result(Status.IN_STOCK, "enabled cart", "browser")
        with mock.patch.object(config, "CONFIRM_BEFORE_ALERT", False), \
             mock.patch.object(notify, "send_reliable", return_value=({"discord": "ok"}, True)), \
             mock.patch.object(stock_monitor, "_confirmation_notice") as followup:
            stock_monitor.process_result(product, state, result, 0.2)
        followup.assert_not_called()

    def test_outbox_file_contains_no_plain_binary(self):
        alert = notify.Alert("image", "body", image_png=b"\x89PNG\r\n")
        with mock.patch.object(notify, "configured_channels", return_value=["discord"]), \
             mock.patch.dict(notify.CHANNELS, {"discord": mock.Mock(side_effect=RuntimeError("down"))}):
            notify.send_reliable(alert, "image:1")
        with open(config.OUTBOX_FILE, encoding="utf-8") as handle:
            raw = handle.read()
        json.loads(raw)
        self.assertIn("image_png", raw)

    def test_watchdog_detects_stale_monitor(self):
        with open(config.RUNTIME_HEARTBEAT_FILE, "w", encoding="utf-8") as handle:
            json.dump({"timestamp": 1, "pid": 123}, handle)
        with mock.patch.object(
            watchdog, "_restart_windows_monitor",
            return_value=(True, "restart requested"),
        ) as restart, mock.patch.object(
            notify, "send", return_value={"discord": "ok"}
        ) as send:
            code = watchdog.check()
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 1)
        restart.assert_called_once_with()
        with open(config.WATCHDOG_STATE_FILE, encoding="utf-8") as handle:
            self.assertTrue(json.load(handle)["was_stale"])

    def test_watchdog_stays_quiet_for_current_heartbeat(self):
        with open(config.RUNTIME_HEARTBEAT_FILE, "w", encoding="utf-8") as handle:
            json.dump({"timestamp": stock_monitor.now(), "pid": 123}, handle)
        with mock.patch.object(notify, "send") as send:
            code = watchdog.check()
        self.assertEqual(code, 0)
        send.assert_not_called()

    def test_example_env_does_not_contain_a_live_discord_secret(self):
        example = os.path.join(os.path.dirname(__file__), ".env.example")
        with open(example, encoding="utf-8") as handle:
            text = handle.read()
        live = re.compile(r"discord(?:app)?\.com/api/webhooks/(?!REPLACE_ME|123)[^/\s]+/[^\s]+")
        self.assertIsNone(live.search(text))

    def test_dns_failure_skips_retries_and_browser(self):
        tls = mock.Mock()
        tls.get.side_effect = RuntimeError("Could not resolve host: example.test")
        product = {"url": "https://example.test/item"}
        with mock.patch.object(detect, "curl_requests", tls), \
             mock.patch.object(detect._session, "get") as plain_get, \
             mock.patch.object(detect, "_browser_check") as browser:
            result = detect.check_product(product)
        self.assertIs(result.status, Status.ERROR)
        plain_get.assert_not_called()
        browser.assert_not_called()

    def test_daily_summary_ignores_disabled_retailers(self):
        products = [
            {"key": "enabled", "name": "Enabled", "enabled": True},
            {"key": "disabled", "name": "Disabled", "enabled": False},
        ]
        state = {
            "products": {
                "enabled": {"last_reading": "error", "status": "out_of_stock"},
                "disabled": {"last_reading": "blocked", "status": "out_of_stock"},
            }
        }
        current_hour = stock_monitor.datetime.now().hour
        with mock.patch.object(config, "PRODUCTS", products), \
             mock.patch.object(config, "HEARTBEAT_ENABLED", True), \
             mock.patch.object(config, "HEARTBEAT_HOUR", current_hour), \
             mock.patch.object(config, "HEARTBEAT_WINDOW_HOURS", 1), \
             mock.patch.object(notify, "has_pending", return_value=False), \
             mock.patch.object(notify, "send_reliable", return_value=({"discord": "ok"}, True)) as send:
            stock_monitor.maybe_heartbeat(state)
        alert = send.call_args.args[0]
        self.assertIn("1 of 1 retailer checks", alert.body)
        self.assertEqual([field[0] for field in alert.fields], ["Enabled"])

    def test_controller_alert_routes_to_dedicated_webhook(self):
        with mock.patch.dict(os.environ, {
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/console",
            "CONTROLLER_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/2/controller",
        }):
            self.assertEqual(notify.configured_channels("console"), ["discord_console"])
            self.assertEqual(notify.configured_channels("controller"), ["discord_controller"])
            self.assertEqual(notify._webhook_for_group("controller"),
                             "https://discord.com/api/webhooks/2/controller")

    def test_controller_alert_falls_back_to_console_webhook(self):
        with mock.patch.dict(os.environ, {
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/console",
            "CONTROLLER_DISCORD_WEBHOOK_URL": "",
        }):
            self.assertEqual(notify.configured_channels("controller"), ["discord_console"])
            self.assertEqual(notify._webhook_for_group("controller"),
                             "https://discord.com/api/webhooks/1/console")

    def test_controller_embed_has_clear_action_and_identity(self):
        alert = notify.Alert(
            "🟡 PRE-ORDER OPEN • Target", "## 🎮 Zelda controller",
            url="https://example.test/controller", urgent=True, group="controller",
        )
        payload = notify._discord_payload(alert)
        self.assertIn("CONTROLLER STOCK WATCH", payload["embeds"][0]["author"]["name"])
        self.assertIn("OPEN PRODUCT", payload["content"])
        self.assertIn("@here", payload["content"])

    def test_live_status_is_split_by_product_group(self):
        products = [
            {"key": "console_target", "name": "Console Target", "enabled": True,
             "group": "console"},
            {"key": "controller_target", "name": "Controller Target", "enabled": True,
             "group": "controller"},
        ]
        state = {"products": {}}
        with mock.patch.object(config, "PRODUCTS", products):
            controller = stock_monitor._status_alert(state, "controller")
        self.assertEqual(controller.group, "controller")
        self.assertEqual([field[0] for field in controller.fields], ["Controller Target"])

    def test_controller_catalog_has_all_verified_retailers(self):
        controllers = {p["name"]: p for p in config.PRODUCTS
                       if p.get("group") == "controller"}
        self.assertEqual(
            set(controllers), {"Target", "Walmart", "GameStop", "Nintendo Store", "Best Buy"}
        )
        self.assertEqual(controllers["Best Buy"]["sku"], "6691849")
        self.assertEqual(controllers["Target"]["tcin"], "1013213521")
        self.assertEqual(controllers["Walmart"]["item_id"], "20954470204")
        self.assertEqual(controllers["Best Buy"]["url"].count("https://"), 1)
        self.assertIn("6691849", controllers["Best Buy"]["status_url"])
        self.assertEqual(controllers["Best Buy"]["http_attempts"], 0)
        self.assertFalse(controllers["Best Buy"]["use_browser"])
        self.assertFalse(controllers["Walmart"]["use_browser"])

    def test_all_primary_retailers_enabled_for_both_product_groups(self):
        expected = {"Target", "Walmart", "GameStop", "Nintendo Store", "Best Buy"}
        for group in ("console", "controller"):
            products = [p for p in config.PRODUCTS if p.get("group") == group]
            enabled = {p["name"] for p in products if p["enabled"]}
            self.assertTrue(expected <= enabled, f"{group} missing {expected - enabled}")

    def test_zero_http_attempts_fail_fast_after_tls_failure(self):
        tls = mock.Mock()
        tls.get.side_effect = RuntimeError("blocked")
        with mock.patch.object(detect, "curl_requests", tls), \
             mock.patch.object(detect._session, "get") as plain:
            body, status, note = detect.fetch("https://example.test", timeout=1, attempts=0)
        self.assertIsNone(body)
        self.assertIsNone(status)
        self.assertIn("blocked", note)
        plain.assert_not_called()

    def test_walmart_retries_http_tls_profile_after_bot_wall(self):
        blocked = mock.Mock(status_code=200, text="Robot or human")
        full_page = mock.Mock(
            status_code=200,
            text='{"availabilityStatus":"OUT_OF_STOCK"}' + ("x" * 6000),
        )
        tls = mock.Mock()
        tls.get.side_effect = [blocked, full_page]
        with mock.patch.object(detect, "curl_requests", tls), \
             mock.patch.object(detect._session, "get") as plain:
            body, status, note = detect.fetch(
                "https://www.walmart.com/ip/20954470204", timeout=1, attempts=0
            )
        self.assertIn("out_of_stock", body.lower())
        self.assertEqual(status, 200)
        self.assertIn("chrome_android", note)
        self.assertEqual(tls.get.call_count, 2)
        plain.assert_not_called()

    def test_macos_installer_builds_a_boot_daemon_without_embedded_secrets(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "install_macos_service.sh"),
                  encoding="utf-8") as handle:
            installer = handle.read()
        with open(os.path.join(root, "macos_service.sh"),
                  encoding="utf-8") as handle:
            helper = handle.read()
        self.assertIn("/Library/LaunchDaemons", installer)
        self.assertIn('"RunAtLoad": True', installer)
        self.assertIn('"KeepAlive": True', installer)
        self.assertIn('"ProcessType": "Background"', installer)
        self.assertIn('"UserName": os.environ["ZSB_USER"]', installer)
        self.assertNotIn("LimitLoadToSessionType", installer)
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", installer)
        self.assertIn("SERVICE_LOG_MAX_BYTES", installer)
        self.assertIn("-m unittest discover", installer)
        self.assertIn("--test-alert", installer)
        self.assertIn("launchctl kickstart", helper)
        self.assertNotRegex(installer, r"discord(?:app)?\.com/api/webhooks/\d+/")

    def test_windows_installer_provisions_browser_dependencies(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "install_windows_tasks.ps1"),
                  encoding="utf-8") as handle:
            installer = handle.read()
        self.assertIn('requirements.txt', installer)
        self.assertIn('requirements-browser.txt', installer)
        self.assertIn('-m playwright install chromium', installer)
        self.assertIn('-m unittest discover', installer)
        self.assertIn('sys.version_info < (3, 11)', installer)
        self.assertIn('"pythonw.exe"', installer)
        self.assertIn('--service-log', installer)
        self.assertIn('-WorkingDirectory $repoDir', installer)
        self.assertNotIn('run_service.ps1', installer)
        self.assertIn('stock_bot_ui.py', installer)
        self.assertIn('CreateShortcut', installer)
        self.assertIn('Stock Watch.lnk', installer)

    def test_windows_ui_stops_watchdog_before_monitor(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "stock_bot_ui.py"), encoding="utf-8") as handle:
            panel = handle.read()
        stop_body = panel.split("def stop_bot()", 1)[1].split("def restart_bot()", 1)[0]
        self.assertLess(stop_body.index("WATCHDOG_TASK, False"),
                        stop_body.index('"/End", "/TN", MONITOR_TASK'))
        self.assertIn("CREATE_NO_WINDOW", panel)
        self.assertIn("_wait_for_pid_exit(old_pid)", panel)
        self.assertIn("Live retailer feed", panel)
        self.assertIn("Add link", panel)
        self.assertIn("product_catalog.save_custom_products", panel)
        self.assertIn("+ Add Product", panel)
        self.assertIn("How it works", panel)
        self.assertIn("--show-guide", panel)

    def test_custom_product_catalog_round_trip_and_retailer_inference(self):
        path = os.path.join(self.tmp.name, "custom-products.json")
        saved = product_catalog.save_custom_products([{
            "url": "https://www.target.com/p/example-item/-/A-123456789",
            "product_name": "Example item",
            "interval": 25,
        }], path)
        loaded, errors = product_catalog.load_custom_products(path)
        self.assertEqual(errors, [])
        self.assertEqual(loaded, saved)
        self.assertEqual(loaded[0]["name"], "Target")
        self.assertEqual(loaded[0]["tcin"], "123456789")
        self.assertTrue(loaded[0]["custom"])

    def test_custom_listing_requires_phrase_and_safe_url(self):
        with self.assertRaisesRegex(ValueError, "needs a phrase"):
            product_catalog.normalize_product({
                "url": "https://example.test/listing", "kind": "appears",
            })
        with self.assertRaisesRegex(ValueError, "complete http"):
            product_catalog.normalize_product({"url": "file:///private/item"})

    def test_custom_walmart_link_gets_exact_item_status_and_cart_urls(self):
        item = product_catalog.normalize_product({
            "url": "https://www.walmart.com/ip/example/21002656445", "interval": 60,
        })
        self.assertEqual(item["item_id"], "21002656445")
        self.assertEqual(item["status_url"],
                         "https://www.walmart.com/search?q=21002656445")
        self.assertIn("items=21002656445", item["cart_url"])
        self.assertFalse(item["use_browser"])

    def test_custom_product_alert_uses_generic_identity(self):
        product = product_catalog.normalize_product({
            "url": "https://shop.example.test/products/limited-widget",
            "product_name": "Limited Widget", "interval": 60,
        })
        alert = stock_monitor.build_alert(
            product, Result(Status.IN_STOCK, "structured stock", "json-ld"), 1,
        )
        self.assertTrue(alert.body.startswith("## 📦 Limited Widget"))
        payload = notify._discord_payload(alert)
        self.assertEqual(payload["embeds"][0]["author"]["name"],
                         "📦 PRODUCT STOCK WATCH")

    def test_walmart_and_bestbuy_never_launch_a_browser(self):
        http_only = [p for p in config.PRODUCTS if p["name"] in {"Walmart", "Best Buy"}]
        self.assertEqual(len(http_only), 4)
        self.assertTrue(all(p.get("use_browser") is False for p in http_only))
        self.assertTrue(all("fresh_headful_browser" not in p for p in http_only))
        walmart = [p for p in http_only if p["name"] == "Walmart"]
        self.assertTrue(all(p.get("status_url", "").startswith(
            "https://www.walmart.com/search?q=") for p in walmart))

    def test_service_logging_writes_to_bounded_log(self):
        path = os.path.join(self.tmp.name, "service.log")
        logger = logging.getLogger("zelda-stock-monitor-service")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            with mock.patch.dict(os.environ, {
                "SERVICE_LOG_FILE": path,
                "SERVICE_LOG_MAX_BYTES": "1024",
                "SERVICE_LOG_BACKUPS": "2",
            }):
                stock_monitor._configure_service_logging()
                print("macOS service log test")
                sys.stdout.flush()
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        with open(path, encoding="utf-8") as handle:
            self.assertIn("macOS service log test", handle.read())

    def test_gamestop_zero_inventory_overrides_stale_jsonld(self):
        body = '''
        <script type="application/ld+json">
        {"@type":"Product","offers":{"@type":"Offer","price":"99.99",
         "availability":"https://schema.org/InStock"}}
        </script>
        <div class="product-availability" data-available="false">
          0 item(s) are available for Pre-Order
        </div>
        '''
        result = detect._retailer_consistency_guard(
            {"url": "https://www.gamestop.com/item"}, body.lower()
        )
        self.assertIs(result.status, Status.OUT_OF_STOCK)
        self.assertEqual(result.price, "$99.99")

    def test_bestbuy_coming_soon_overrides_stale_jsonld(self):
        body = '''
        <script type="application/ld+json">
        {"@type":"Product","offers":{"@type":"Offer","price":"99.99",
         "availability":"https://schema.org/InStock"}}
        </script>
        {"skuid":"6691849","fulfillmentoptions":{"buttonstates":[
          {"buttonstate":"coming_soon","displaytext":"coming soon"}
        ]}}
        '''
        result = detect._retailer_consistency_guard(
            {"url": "https://www.bestbuy.com/product/item", "sku": "6691849"},
            body.lower(),
        )
        self.assertIs(result.status, Status.OUT_OF_STOCK)
        self.assertEqual(result.source, "bestbuy-product-state")
        self.assertIn("coming_soon", result.reason)
        self.assertEqual(result.price, "$99.99")

    def test_bestbuy_server_rendered_exact_sku_button(self):
        body = '''
        <button class="add-to-cart-button" disabled
          data-sku-id="6691841" data-button-state="coming_soon">
          Coming Soon
        </button>
        <button data-sku-id="other-sku" data-button-state="add_to_cart">
          Add to Cart
        </button>
        '''
        result = detect._retailer_consistency_guard(
            {"url": "https://www.bestbuy.com/product/item", "sku": "6691841"},
            body.lower(),
        )
        self.assertIs(result.status, Status.OUT_OF_STOCK)
        self.assertEqual(result.source, "bestbuy-product-button")

    def test_bestbuy_exact_sku_see_details_is_not_buyable(self):
        body = '''
        <a role="button" data-sku-id="6691841"
          data-button-state="see_details" href="/product/item">
          See details
        </a>
        '''
        result = detect._retailer_consistency_guard(
            {"url": "https://www.bestbuy.com/product/item", "sku": "6691841"},
            body.lower(),
        )
        self.assertIs(result.status, Status.OUT_OF_STOCK)
        self.assertEqual(result.source, "bestbuy-product-button")

    def test_walmart_item_scoped_state_ignores_recommendations(self):
        body = '''
        {"availabilityStatus":"IN_STOCK",
         "canonicalUrl":"/ip/recommended-controller/111111"}
        {"availabilityStatus":"OUT_OF_STOCK",
         "preorder":{"isPreorder":true},
         "canonicalUrl":"/ip/zelda-controller/20954470204",
         "usItemId":"20954470204"}
        '''
        result = detect._retailer_consistency_guard(
            {"url": "https://www.walmart.com/ip/20954470204", "item_id": "20954470204"},
            body.lower(),
        )
        self.assertIs(result.status, Status.OUT_OF_STOCK)
        self.assertEqual(result.source, "walmart-product-state")

    def test_bestbuy_guard_uses_only_requested_skus_button_state(self):
        body = '''
        {"skuid":"other-sku","fulfillmentoptions":{"buttonstates":[
          {"buttonstate":"coming_soon"}
        ]}}
        {"skuid":"6691849","fulfillmentoptions":{"buttonstates":[
          {"buttonstate":"add_to_cart"}
        ]}}
        '''
        result = detect._retailer_consistency_guard(
            {"url": "https://www.bestbuy.com/product/item", "sku": "6691849"},
            body.lower(),
        )
        self.assertIs(result.status, Status.IN_STOCK)
        self.assertEqual(result.source, "bestbuy-product-state")
        self.assertIn("add_to_cart", result.reason)

    def test_bestbuy_stale_jsonld_without_sku_state_fails_closed(self):
        body = '''
        <script type="application/ld+json">
        {"@type":"Product","offers":{"@type":"Offer","price":"519.99",
         "availability":"https://schema.org/InStock"}}
        </script>
        ''' + ("x" * 6000)
        product = {
            "url": "https://www.bestbuy.com/product/item",
            "sku": "6691841",
            "use_browser": False,
        }
        with mock.patch.object(detect, "fetch", return_value=(body.lower(), 200, "ok")):
            result = detect.check_product(product)
        self.assertIs(result.status, Status.UNKNOWN)
        self.assertEqual(result.source, "bestbuy-unverified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
