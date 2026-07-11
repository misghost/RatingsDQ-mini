"""E2E 验证用户管理接口（生产 df.ratings.ink）。
admin 身份：header X-Openid: admin
"""
import json
import urllib.request
import urllib.error

BASE = "https://df.ratings.ink"
ADMIN = "admin"
HDR = {"X-Openid": ADMIN, "Content-Type": "application/json"}


def call(method, path, body=None):
    sep = "&" if "?" in path else "?"
    url = BASE + path + sep + "openid=" + ADMIN
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": e.reason}


def find_target():
    s, b = call("GET", "/api/admin/users")
    assert s == 200, f"list failed {s}"
    users = b["users"]
    for u in users:
        if u["role"] != "admin":
            return u["openid"], u
    return None, None


def main():
    print("== 1) 拉取用户列表，选一个市场人员作为测试目标 ==")
    T, info = find_target()
    assert T, "没有可用于测试的市场人员账号"
    print("  目标:", T, info.get("name"))

    print("== 2) PUT 修改资料（改名+改机构）==")
    s, b = call("PUT", f"/api/admin/users/{T}",
                {"name": "E2E测试改名", "organization": "E2E测试机构",
                 "phone": info.get("phone") or "13800000000", "email": "e2e@example.com"})
    print("  ", s, b)
    assert s == 200, "PUT 失败"

    print("== 3) POST 停用 ==")
    s, b = call("POST", f"/api/admin/users/{T}/disable")
    print("  ", s, b)
    assert s == 200 and b.get("status") == "disabled", "停用失败"

    print("== 4) 列表确认 status=disabled ==")
    s, b = call("GET", "/api/admin/users")
    cur = next((u for u in b["users"] if u["openid"] == T), None)
    assert cur and cur["status"] == "disabled", "列表状态未更新"
    print("  OK, 当前状态:", cur["status"], "姓名:", cur["name"])

    print("== 5) POST 启用 ==")
    s, b = call("POST", f"/api/admin/users/{T}/enable")
    print("  ", s, b)
    assert s == 200 and b.get("status") == "approved", "启用失败"

    print("== 6) DELETE 软删除 ==")
    s, b = call("DELETE", f"/api/admin/users/{T}")
    print("  ", s, b)
    assert s == 200, "删除失败"

    print("== 7) 回收站确认在列 ==")
    s, b = call("GET", "/api/admin/users?scope=deleted")
    trash = [u for u in b["users"] if u["openid"] == T]
    assert trash, "回收站未找到"
    print("  OK, 回收站命中:", trash[0]["name"])

    print("== 8) POST 恢复 ==")
    s, b = call("POST", f"/api/admin/users/{T}/restore")
    print("  ", s, b)
    assert s == 200 and b.get("status") == "approved", "恢复失败"

    print("== 9) 列表确认已回到正常 ==")
    s, b = call("GET", "/api/admin/users")
    cur = next((u for u in b["users"] if u["openid"] == T), None)
    assert cur and cur["status"] == "approved", "恢复后未回到正常"
    print("  OK, 状态:", cur["status"], "姓名:", cur["name"])

    print("== 10) 自保护：删除自己(admin) 应 400 ==")
    s, b = call("DELETE", f"/api/admin/users/{ADMIN}")
    print("  ", s, b)
    assert s == 400, "未拦截删除自己"

    print("== 11) 自保护：停用自己(admin) 应 400 ==")
    s, b = call("POST", f"/api/admin/users/{ADMIN}/disable")
    print("  ", s, b)
    assert s == 400, "未拦截停用自己"

    # 把改名恢复回来，避免脏数据
    print("== 12) 还原测试目标姓名/机构 ==")
    s, b = call("PUT", f"/api/admin/users/{T}",
                {"name": info.get("name") or "", "organization": info.get("organization") or "",
                 "phone": info.get("phone") or "", "email": info.get("email") or ""})
    print("  ", s, b.get("message"))

    print("\n✅ 全部 E2E 用例通过")


if __name__ == "__main__":
    main()
