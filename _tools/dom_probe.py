"""在独立 WebView2 进程里读取页面 DOM 事实。

为什么要单独一层：直连 /api 的用例只能证明服务端是对的，证明不了「GUI 页面本身
能用」。本项目已经踩过两次：令牌占位符被 str.replace 连变量名一起替换、
.modal/.drawer 的 display 盖掉 [hidden]。这两类问题只有看真实 DOM 才能发现。

用法： from dom_probe import probe; d = probe("http://127.0.0.1:8760/")
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_CHILD = r'''
import os, sys, time, webview
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
os.environ["no_proxy"] = "127.0.0.1,localhost,::1"
url = sys.argv[1]
w = webview.create_window("dom-probe", url=url, width=420, height=260)
expr = r"""JSON.stringify((function(){
  var hid = function(id){ var e=document.getElementById(id);
    return e ? (e.hidden && getComputedStyle(e).display === 'none') : null; };
  var t = document.getElementById('toast');
  var ls = document.getElementById('f-locale');
  var lc = document.getElementById('f-locale-custom');
  var opts = ls ? [].slice.call(ls.options).map(function(o){ return o.value; }) : [];
  var lcHidden = function(){ return lc ? (lc.hidden && getComputedStyle(lc).display === 'none') : null; };
  var before = lcHidden();
  var toggle = null, payload = null;
  if (ls && lc) {
    try {
      ls.value = '__custom__'; ls.dispatchEvent(new Event('change'));
      var shown = !lcHidden();
      lc.value = 'fr-CA';
      var p1 = (typeof formPayload === 'function') ? formPayload().locale : null;
      ls.value = 'zh-CN'; ls.dispatchEvent(new Event('change'));
      var hidden = lcHidden();
      var p2 = (typeof formPayload === 'function') ? formPayload().locale : null;
      toggle = shown && hidden;
      payload = (p1 === 'fr-CA' && p2 === 'zh-CN');
      ls.value = 'en-US'; ls.dispatchEvent(new Event('change'));
    } catch (e) { toggle = 'ERR:' + e.message; payload = 'ERR:' + e.message; }
  }
  return {
    token: String(window.__FP_TOKEN__),
    drawerHidden: hid('drawer'),
    toastHidden: hid('toast'),
    toastText: t ? t.textContent : '',
    onboardingHidden: hid('onboarding'),
    visibleViews: [].slice.call(document.querySelectorAll('.view'))
      .filter(function(e){ return !e.hidden; }).length,
    theme: document.documentElement.getAttribute('data-theme'),
    localeTag: ls ? ls.tagName : null,
    localeOptionCount: opts.length,
    localeGroups: ls ? [].slice.call(ls.querySelectorAll('optgroup')).map(function(g){ return g.label; }) : [],
    localeHasEnUS: opts.indexOf('en-US') >= 0,
    localeHasZhCN: opts.indexOf('zh-CN') >= 0,
    localeHasJaJP: opts.indexOf('ja-JP') >= 0,
    localeHasCustom: opts.indexOf('__custom__') >= 0,
    localeCustomHiddenBefore: before,
    localeToggle: toggle,
    localePayload: payload
  };
})())"""
def loaded():
    time.sleep(2.0)
    try:
        print("__FP_DOM__" + str(w.evaluate_js(expr)), flush=True)
    except Exception as e:
        print("__FP_DOM_ERR__" + type(e).__name__ + ": " + str(e), flush=True)
    sys.stdout.flush()
    os._exit(0)
w.events.loaded += loaded
webview.start(gui="edgechromium", debug=False)
'''


def probe(url: str, timeout: int = 45) -> dict:
    """返回 DOM 事实字典；失败时返回 {'error': ...}。不改动任何状态。"""
    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = "127.0.0.1,localhost,::1"
    try:
        cp = subprocess.run(
            [sys.executable, "-c", _CHILD, url],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env,
        )
        for line in (cp.stdout or "").splitlines():
            if line.startswith("__FP_DOM__"):
                return json.loads(line[len("__FP_DOM__"):])
            if line.startswith("__FP_DOM_ERR__"):
                return {"error": line[len("__FP_DOM_ERR__"):]}
        return {"error": f"无 DOM 输出 exit={cp.returncode} stderr={(cp.stderr or '')[:200]}"}
    except subprocess.TimeoutExpired:
        return {"error": f"WebView2 探测超时 {timeout}s"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
