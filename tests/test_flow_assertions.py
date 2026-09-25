"""
Unit tests for the test-flow assertion evaluator (pure function).

Covers:
  - each field/param op: equals, contains, regex, exists, not_empty, default
  - dataLayer must / must_not
  - vendor must / must_not
  - anytime vs at_step scoping
  - total / passed accounting and per-step structure
"""

from app.tag_testing.flow_runner.assertions import check_op, evaluate

_MISSING = object()


# ── Operator-level tests ──────────────────────────────────────────────────────


class TestCheckOp:
    def test_equals_pass(self):
        assert check_op("equals", "purchase", "purchase") is True

    def test_equals_fail(self):
        assert check_op("equals", "view", "purchase") is False

    def test_equals_numeric_coercion(self):
        # 9.0 float normalises to "9"
        assert check_op("equals", 9.0, "9") is True

    def test_equals_bool_coercion(self):
        assert check_op("equals", True, "true") is True

    def test_contains_pass(self):
        assert check_op("contains", "add_to_cart", "cart") is True

    def test_contains_fail(self):
        assert check_op("contains", "checkout", "cart") is False

    def test_regex_pass(self):
        assert check_op("regex", "USD-99", r"USD-\d+") is True

    def test_regex_fail(self):
        assert check_op("regex", "abc", r"\d+") is False

    def test_regex_invalid_pattern_fails_closed(self):
        assert check_op("regex", "abc", r"[") is False

    def test_exists_present(self):
        assert check_op("exists", "anything", None) is True

    def test_exists_missing(self):
        from app.tag_testing.flow_runner.assertions import _MISSING as missing

        assert check_op("exists", missing, None) is False

    def test_not_empty_pass(self):
        assert check_op("not_empty", "x", None) is True

    def test_not_empty_blank_fails(self):
        assert check_op("not_empty", "   ", None) is False

    def test_not_empty_missing_fails(self):
        from app.tag_testing.flow_runner.assertions import _MISSING as missing

        assert check_op("not_empty", missing, None) is False

    def test_default_op_no_value_is_exists(self):
        # No op + no expected value → existence check
        assert check_op(None, "present", None) is True
        from app.tag_testing.flow_runner.assertions import _MISSING as missing

        assert check_op(None, missing, None) is False

    def test_default_op_with_value_is_equals(self):
        assert check_op(None, "purchase", "purchase") is True
        assert check_op(None, "view", "purchase") is False

    def test_op_requiring_present_fails_when_missing(self):
        from app.tag_testing.flow_runner.assertions import _MISSING as missing

        assert check_op("contains", missing, "x") is False


# ── Helpers ───────────────────────────────────────────────────────────────────


def _dl_assert(event, mode="must", when="anytime", fields=None):
    return {"event": event, "mode": mode, "when": when, "fields": fields or []}


def _vendor_assert(vendor_id, mode="must", when="anytime", params=None):
    return {"vendor_id": vendor_id, "mode": mode, "when": when, "params": params or []}


def _step(action="navigate", datalayer=None, vendor=None):
    return {
        "action": action,
        "assertions": {
            "datalayer_events": datalayer or [],
            "vendor_requests": vendor or [],
        },
    }


# ── dataLayer must / must_not ─────────────────────────────────────────────────


class TestDataLayerMust:
    def test_must_fire_passes_when_present(self):
        steps = [_step(datalayer=[_dl_assert("purchase")])]
        execution = {
            "datalayer_events": [{"event": "purchase", "data": {}, "step_index": 0}],
            "beacons": [],
        }
        result = evaluate(steps, execution)
        assert result["total"] == 1
        assert result["passed"] == 1
        assert result["per_step"][0]["results"][0]["passed"] is True

    def test_must_fire_fails_when_absent(self):
        steps = [_step(datalayer=[_dl_assert("purchase")])]
        execution = {"datalayer_events": [], "beacons": []}
        result = evaluate(steps, execution)
        assert result["total"] == 1
        assert result["passed"] == 0

    def test_must_not_passes_when_absent(self):
        steps = [_step(datalayer=[_dl_assert("purchase", mode="must_not")])]
        execution = {"datalayer_events": [], "beacons": []}
        result = evaluate(steps, execution)
        assert result["passed"] == 1

    def test_must_not_fails_when_present(self):
        steps = [_step(datalayer=[_dl_assert("purchase", mode="must_not")])]
        execution = {
            "datalayer_events": [{"event": "purchase", "data": {}, "step_index": 0}],
            "beacons": [],
        }
        result = evaluate(steps, execution)
        assert result["passed"] == 0

    def test_field_check_must_match(self):
        steps = [
            _step(
                datalayer=[
                    _dl_assert(
                        "purchase",
                        fields=[{"key": "value", "op": "equals", "value": "9.99"}],
                    )
                ]
            )
        ]
        execution = {
            "datalayer_events": [{"event": "purchase", "data": {"value": "9.99"}, "step_index": 0}],
            "beacons": [],
        }
        assert evaluate(steps, execution)["passed"] == 1

    def test_field_check_wrong_value_fails(self):
        steps = [
            _step(
                datalayer=[
                    _dl_assert(
                        "purchase",
                        fields=[{"key": "value", "op": "equals", "value": "10.00"}],
                    )
                ]
            )
        ]
        execution = {
            "datalayer_events": [{"event": "purchase", "data": {"value": "9.99"}, "step_index": 0}],
            "beacons": [],
        }
        assert evaluate(steps, execution)["passed"] == 0


# ── anytime vs at_step scoping ────────────────────────────────────────────────


class TestScoping:
    def test_anytime_matches_across_steps(self):
        # assertion on step 0, event fired during step 1
        steps = [
            _step(datalayer=[_dl_assert("purchase", when="anytime")]),
            _step(action="click"),
        ]
        execution = {
            "datalayer_events": [{"event": "purchase", "data": {}, "step_index": 1}],
            "beacons": [],
        }
        assert evaluate(steps, execution)["passed"] == 1

    def test_at_step_only_matches_same_step(self):
        steps = [
            _step(datalayer=[_dl_assert("purchase", when="at_step")]),
            _step(action="click"),
        ]
        # fired in step 1, but assertion is at_step on step 0 → fail
        execution = {
            "datalayer_events": [{"event": "purchase", "data": {}, "step_index": 1}],
            "beacons": [],
        }
        assert evaluate(steps, execution)["passed"] == 0

    def test_at_step_matches_when_same_step(self):
        steps = [_step(datalayer=[_dl_assert("purchase", when="at_step")])]
        execution = {
            "datalayer_events": [{"event": "purchase", "data": {}, "step_index": 0}],
            "beacons": [],
        }
        assert evaluate(steps, execution)["passed"] == 1


# ── vendor requests ───────────────────────────────────────────────────────────


class TestVendorRequests:
    def test_vendor_must_fire(self):
        steps = [_step(vendor=[_vendor_assert("v1")])]
        execution = {
            "datalayer_events": [],
            "beacons": [{"vendor_id": "v1", "params": {}, "step_index": 0}],
        }
        assert evaluate(steps, execution)["passed"] == 1

    def test_vendor_must_fire_wrong_vendor_fails(self):
        steps = [_step(vendor=[_vendor_assert("v1")])]
        execution = {
            "datalayer_events": [],
            "beacons": [{"vendor_id": "v2", "params": {}, "step_index": 0}],
        }
        assert evaluate(steps, execution)["passed"] == 0

    def test_vendor_must_not_passes_when_absent(self):
        steps = [_step(vendor=[_vendor_assert("v1", mode="must_not")])]
        execution = {"datalayer_events": [], "beacons": []}
        assert evaluate(steps, execution)["passed"] == 1

    def test_vendor_param_check(self):
        steps = [
            _step(
                vendor=[
                    _vendor_assert(
                        "v1",
                        params=[{"key": "en", "op": "equals", "value": "purchase"}],
                    )
                ]
            )
        ]
        execution = {
            "datalayer_events": [],
            "beacons": [{"vendor_id": "v1", "params": {"en": "purchase"}, "step_index": 0}],
        }
        assert evaluate(steps, execution)["passed"] == 1

    def test_vendor_param_exists_default(self):
        # No value → exists check
        steps = [_step(vendor=[_vendor_assert("v1", params=[{"key": "tid"}])])]
        execution = {
            "datalayer_events": [],
            "beacons": [{"vendor_id": "v1", "params": {"tid": "G-XYZ"}, "step_index": 0}],
        }
        assert evaluate(steps, execution)["passed"] == 1

    def test_vendor_param_exists_default_missing_fails(self):
        steps = [_step(vendor=[_vendor_assert("v1", params=[{"key": "tid"}])])]
        execution = {
            "datalayer_events": [],
            "beacons": [{"vendor_id": "v1", "params": {"other": "x"}, "step_index": 0}],
        }
        assert evaluate(steps, execution)["passed"] == 0


# ── accounting + structure ────────────────────────────────────────────────────


class TestAccounting:
    def test_totals_and_structure(self):
        steps = [
            _step(
                datalayer=[_dl_assert("purchase"), _dl_assert("view", mode="must_not")],
                vendor=[_vendor_assert("v1")],
            )
        ]
        execution = {
            "datalayer_events": [{"event": "purchase", "data": {}, "step_index": 0}],
            "beacons": [{"vendor_id": "v1", "params": {}, "step_index": 0}],
        }
        result = evaluate(steps, execution)
        assert result["total"] == 3
        assert result["passed"] == 3
        assert len(result["per_step"]) == 1
        assert result["per_step"][0]["step_index"] == 0
        kinds = {r["kind"] for r in result["per_step"][0]["results"]}
        assert kinds == {"datalayer", "vendor"}

    def test_empty_steps(self):
        result = evaluate([], {"datalayer_events": [], "beacons": []})
        assert result == {"total": 0, "passed": 0, "per_step": []}

    def test_step_with_no_assertions(self):
        result = evaluate([_step()], {"datalayer_events": [], "beacons": []})
        assert result["total"] == 0
        assert result["per_step"][0]["results"] == []
