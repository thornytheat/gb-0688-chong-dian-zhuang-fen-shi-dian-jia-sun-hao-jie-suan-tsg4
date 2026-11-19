from flask import Blueprint, request, jsonify
from app.pipeline import run
from app.errors import DomainError
from app import store
from . import service

bp = Blueprint("m8", __name__)


def _payload():
    payload = request.get_json(silent=True) if request.method == "POST" else {}
    if not isinstance(payload, dict):
        raise DomainError("BODY", "请求必须是对象")
    raw = payload.get("text")
    if raw is not None and not isinstance(raw, str):
        raise DomainError("TEXT", "数据必须为文本")
    return raw, payload.get("params")


@bp.route("/api/m8", methods=["GET", "POST"])
def preview():
    """同站按日日结预览：完整重算但不落日结库。"""
    raw, params = _payload()
    return jsonify(run(raw, params, stop=8, persist=False))


@bp.route("/api/m8/settle", methods=["POST"])
def settle():
    """显式入库：同站同日唯一；晚到放电单触发重算，失败恢复旧版，重试幂等。"""
    raw, params = _payload()
    ctx = run(raw, params, stop=8, persist=False)
    saved, actions, failures, digest = service.persist(ctx["records"], ctx["manifest"]["rules"])
    body = {"actions": actions, "failures": failures, "daily": saved,
            "flows": ctx["daily_flows"], "input_hash": digest,
            "events": store.list_events()}
    if failures:
        return jsonify({"code": "DAILY_ROLLBACK", "message": "部分日结重算失败，已恢复旧版日结", **body}), 409
    return jsonify(body)


@bp.route("/api/m8/daily")
def daily_list():
    return jsonify(store.list_daily())


@bp.route("/api/m8/daily/<station>/<day>")
def daily_detail(station, day):
    row = store.fetch_daily(station, day)
    row["versions"] = store.list_versions(station, day)
    row["events"] = store.list_events(station, day)
    return jsonify(row)


@bp.route("/api/m8/daily/<station>/<day>/versions/<int:version>")
def daily_version(station, day, version):
    return jsonify(store.fetch_version(station, day, version))


@bp.route("/api/m8/events")
def events():
    return jsonify(store.list_events(request.args.get("station"), request.args.get("day")))


@bp.route("/api/m8/review/<station>/<day>", methods=["POST"])
def review(station, day):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainError("BODY", "请求必须是对象")
    return jsonify(store.review_daily(station, day, data.get("version"), data.get("decision")))
