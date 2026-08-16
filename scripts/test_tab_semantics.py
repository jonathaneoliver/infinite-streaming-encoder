#!/usr/bin/env python3
"""The tabs stay keyboard-operable, and the page keeps its live region (#355).

Before #355 the tabs were `<div class="tab" onclick=…>`: not focusable, not
announced as tabs, and with `tabindex=` appearing ZERO times in the whole page
there was no way to change view without a mouse. The fix is a handful of
attributes, which is exactly the kind of fix that a later edit removes without
noticing — the app looks and behaves identically to someone using a mouse, and
the regression is only visible to the people who cannot report it.

So this pins the attributes rather than the behaviour. Real keyboard interaction
needs a browser and is verified by driving the page; what a static check can do
is refuse the markup that made interaction impossible.

Scope is the TABLIST. The app's other clickable spans (btn-play, btn-fetch,
out-chev, the sort headers) are named as out of scope in #355 — they carry a
title so they are at least named, and making them all focusable is a separate
decision about tab-order length, not a labelling one. A check that failed on all
of them today would simply be disabled.
"""
import os
import sys
from html.parser import HTMLParser

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "index.html")


class Tabs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tablist = None
        self.tabs = []          # (attrs, text)
        self.panels = []        # attrs
        self.live = []          # attrs of aria-live elements
        self._in_tab = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if "tabs" in cls:
            self.tablist = a
        if "tab" in cls:
            self.tabs.append([a, ""])
            self._in_tab = self.tabs[-1]
        if "tab-content" in cls:
            self.panels.append(a)
        if a.get("aria-live"):
            self.live.append(a)

    def handle_data(self, data):
        if self._in_tab is not None and data.strip():
            self._in_tab[1] = (self._in_tab[1] + " " + data.strip()).strip()

    def handle_endtag(self, tag):
        if tag == "div" and self._in_tab is not None:
            self._in_tab = None


def main():
    with open(PAGE, encoding="utf-8") as f:
        html = f.read()

    p = Tabs()
    p.feed(html)
    bad = []

    if not p.tabs:
        print("FAIL: found no .tab elements — this check is looking at the wrong "
              "thing, which is worse than failing")
        return 1

    if not p.tablist or p.tablist.get("role") != "tablist":
        bad.append('.tabs has no role="tablist" — the tabs are not announced as a group')
    elif not (p.tablist.get("aria-label") or p.tablist.get("aria-labelledby")):
        bad.append('the tablist has no accessible name (aria-label)')

    panel_ids = {a.get("id") for a in p.panels if a.get("id")}
    selected = 0
    in_tab_order = 0
    for a, text in p.tabs:
        who = a.get("id") or text or "?"
        if a.get("role") != "tab":
            bad.append('%-22s has no role="tab"' % who)
        if not a.get("id"):
            bad.append('%-22s has no id — the panel cannot point back at it' % who)
        if a.get("aria-selected") is None:
            bad.append('%-22s has no aria-selected — nothing says which view is open' % who)
        elif a["aria-selected"] == "true":
            selected += 1
        # The whole point: without tabindex the element cannot be reached at all.
        ti = a.get("tabindex")
        if ti is None:
            bad.append('%-22s has no tabindex — it cannot be focused, which is '
                       'the bug #355 was opened about' % who)
        elif ti == "0":
            in_tab_order += 1
        ctrl = a.get("aria-controls")
        if not ctrl:
            bad.append('%-22s has no aria-controls' % who)
        elif ctrl not in panel_ids:
            bad.append('%-22s aria-controls="%s" points at no .tab-content' % (who, ctrl))

    # Roving tabindex: EXACTLY one tab is in the page's tab order. More than one
    # and Tab walks the tablist instead of stepping over it; none and the
    # tablist cannot be reached by Tab at all.
    if in_tab_order != 1:
        bad.append("%d tabs have tabindex=0; roving tabindex needs exactly 1 "
                   "(the selected one)" % in_tab_order)
    if selected != 1:
        bad.append('%d tabs have aria-selected="true"; exactly 1 view is open' % selected)

    tab_ids = {a.get("id") for a, _ in p.tabs if a.get("id")}
    for a in p.panels:
        who = a.get("id") or "?"
        if a.get("role") != "tabpanel":
            bad.append('%-22s has no role="tabpanel"' % who)
        lb = a.get("aria-labelledby")
        if not lb:
            bad.append('%-22s has no aria-labelledby — the panel is unnamed' % who)
        elif lb not in tab_ids:
            bad.append('%-22s aria-labelledby="%s" points at no tab' % (who, lb))

    # The live region, and the rule about it. `assertive` interrupts whatever is
    # being read; on a page that repaints every 2s that is hostile, and nothing
    # here is urgent enough to earn it.
    if not p.live:
        bad.append("the page has no aria-live region — state changes are silent "
                   "again, which is the other half of #355")
    for a in p.live:
        if a.get("aria-live") != "polite":
            bad.append('aria-live="%s" on #%s — only polite is acceptable here'
                       % (a.get("aria-live"), a.get("id") or "?"))

    for needed in ("function onTabKey", "function announce", "ArrowRight", "focus-visible"):
        if needed not in html:
            bad.append("%s is gone — the attributes are inert without it" % needed)

    if bad:
        print("FAIL: %d problem(s) with the tablist or live region (#355)" % len(bad))
        for b in bad:
            print("  " + b)
        return 1

    print("ok (%d tabs, %d panels, roving tabindex, %d polite live region(s))"
          % (len(p.tabs), len(p.panels), len(p.live)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
