# -*- coding: utf-8 -*-
"""分享链接参数透传中间件。

为什么需要这一层
----------------
线上沙盒（WorkBuddy 应用发布 / 各类带访问码的托管平台）把整个应用挂在一个
带 ``?sharecode=`` 的入口后面：**每次请求都必须带这个参数**，否则边缘网关直接
返回「链接不完整」，请求根本到不了应用。

而应用内部的跳转是普通相对链接（``/m``、``/admin``、``fetch('/api/v1/chat')``），
一旦点击就把 ``?sharecode`` 丢了，表现为「首页能开，其他页面全打不开」。
这不是应用本身的问题，但用户看到的就是「只有一个页面能用」。

做法
----
对 ``text/html`` 响应注入一小段脚本，做三件事：
1. 把当前 URL 里的 sharecode 记到 sessionStorage / Cookie（页面内跳转后仍可复用）；
2. 给所有 ``<a href>`` 与 ``<form action>`` 补上 sharecode；
3. 包一层 ``fetch``，给同源请求补 sharecode（否则前端调接口会拿到 HTML 报错页）。

只在 ``text/html`` 上生效，SSE 流式响应与 JSON 原样放行，不影响问答的流式输出。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

#: 注入到 </body> 前的脚本。ES5 写法，兼容现场端的旧版浏览器。
_SHARE_JS = r"""
(function () {
  var KEY = 'qt_sharecode';
  var m = location.search.match(/[?&]sharecode=([^&#]+)/);
  var code = (m && m[1]) || sessionStorage.getItem(KEY);
  if (m && m[1]) {
    try { sessionStorage.setItem(KEY, m[1]); } catch (e) {}
    try { document.cookie = KEY + '=' + m[1] + '; path=/; max-age=86400'; } catch (e) {}
  }
  if (!code) return;

  function fix(u) {
    if (!u || typeof u !== 'string') return u;
    if (u.indexOf('#') === 0) return u;
    if (/^(mailto:|tel:|javascript:|data:)/i.test(u)) return u;
    if (/^https?:\/\//i.test(u) && u.indexOf(location.host) === -1) return u;
    if (u.indexOf('sharecode=') !== -1) return u;
    return u + (u.indexOf('?') === -1 ? '?' : '&') + 'sharecode=' + encodeURIComponent(code);
  }

  function patch() {
    var i, els = document.querySelectorAll('a[href]');
    for (i = 0; i < els.length; i++) els[i].setAttribute('href', fix(els[i].getAttribute('href')));
    els = document.querySelectorAll('form[action]');
    for (i = 0; i < els.length; i++) els[i].setAttribute('action', fix(els[i].getAttribute('action')));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', patch);
  } else {
    patch();
  }

  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      try {
        if (typeof input === 'string') {
          input = fix(input);
        } else if (input && input.url && /^https?:\/\//i.test(input.url)
                   && input.url.indexOf(location.host) !== -1) {
          input = new Request(fix(input.url), input);
        }
      } catch (e) {}
      return origFetch.call(this, input, init);
    };
  }

  var open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    var args = Array.prototype.slice.call(arguments);
    args[1] = fix(url);
    return open.apply(this, args);
  };

  // 表单 GET 提交同理：submit 前补参数（action 已在 patch 里改过，这里兜底动态表单）
  document.addEventListener('submit', function (ev) {
    var f = ev.target;
    if (f && f.tagName === 'FORM' && (f.method || 'get').toLowerCase() === 'get') {
      f.setAttribute('action', fix(f.getAttribute('action') || location.pathname));
    }
  }, true);
})();
"""


class ShareLinkMiddleware(BaseHTTPMiddleware):
    """把入口的 sharecode 参数续到页面内所有跳转与接口请求上。"""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        ctype = response.headers.get("content-type", "")
        if "text/html" not in ctype:
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        charset = getattr(response, "charset", None) or "utf-8"
        try:
            html = body.decode(charset, "replace")
        except LookupError:
            html = body.decode("utf-8", "replace")

        if "</body>" in html:
            html = html.replace("</body>", "<script>%s</script></body>" % _SHARE_JS, 1)
        else:
            html += "<script>%s</script>" % _SHARE_JS

        headers = {
            k: v for k, v in response.headers.items()
            if k.lower() not in ("content-length", "content-encoding")
        }
        return Response(
            content=html.encode("utf-8"),
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )
