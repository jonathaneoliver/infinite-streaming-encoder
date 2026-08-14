#!/usr/bin/env python3
"""Every control in the encode form describes itself (#349).

The descriptions have three readers — the on-screen hint, `aria-describedby`,
and the demo recorder — but only ONE writer: a `data-desc` attribute in
`static/index.html`. That is the whole point of the design, and it is also its
weakness: a control added without one is wired to nothing and fails silently in
all three places at once. The recorder walks the DOM and would simply not
mention it; a screen reader would announce a name with no purpose; the hint
would be blank. Nothing errors.

So this is the gate. Add a control to `<div class="controls">` without a
`data-desc` and the build fails here, with the control named.

Scope is the ENCODE FORM only (`<div class="controls">`). The ladder editor's
fields are a separate form, built by JS from a different template, and #349 does
not claim them — extending this there is follow-up work, not a silent omission.
"""
import os
import sys
from html.parser import HTMLParser

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "index.html")

# Controls that are deliberately not described, with the reason. Empty on
# purpose: an exemption list that starts populated is one nobody ever empties.
EXEMPT = {}


class Controls(HTMLParser):
    """Collect the form controls inside <div class="controls">.

    A depth counter rather than a regex slice: the block contains nested divs,
    and matching the closing tag by counting is the only way to know where it
    ends. Controls found outside it are ignored entirely.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0          # div nesting inside .controls; 0 = outside
        self.controls = []      # (tag, ident, attrs)
        self.selects = []       # (ident, [option descs])
        self._cur_select = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "controls" in (a.get("class") or "").split():
                self.depth = 1
        if not self.depth:
            return

        ident = a.get("id") or a.get("name") or a.get("value") or "?"
        if tag in ("select", "input", "textarea"):
            # Buttons and the encode split-menu are actions, not options; they
            # are outside .controls anyway, but be explicit.
            if a.get("type") in ("button", "submit", "hidden"):
                return
            self.controls.append((tag, ident, a))
            if tag == "select":
                self._cur_select = [ident, []]
                self.selects.append(self._cur_select)
        elif tag == "option" and self._cur_select:
            self._cur_select[1].append(a.get("data-desc"))
        elif tag == "div" and a.get("role") == "group":
            # A container standing in for a set of dynamically-built checkboxes
            # (Codec, Ladder). It carries the group-level description.
            self.controls.append(("group", ident, a))

    def handle_endtag(self, tag):
        if tag == "select":
            self._cur_select = None
        if tag == "div" and self.depth:
            self.depth -= 1


def main():
    with open(PAGE, encoding="utf-8") as f:
        html = f.read()

    p = Controls()
    p.feed(html)

    if not p.controls:
        print("FAIL: found no controls inside <div class=\"controls\"> — the "
              "parser is wrong, not the page")
        return 1

    # Selects whose OPTIONS carry the real text. Their control-level data-desc
    # is a one-line header — the visible hint shows the selected option — so the
    # length rule below applies to the options instead, not to the header.
    per_option = {ident for ident, descs in p.selects if any(descs)}

    bad = []
    MIN = 25

    def check_len(what, ident, desc):
        # A three-word description is a label, not a description: it passes a
        # presence check while telling the reader nothing.
        if len(desc) < MIN:
            bad.append("%-8s %-20s data-desc is too short to be a description: %r"
                       % (what, ident, desc))

    for tag, ident, a in p.controls:
        if ident in EXEMPT:
            continue
        desc = (a.get("data-desc") or "").strip()
        if not desc:
            bad.append("%-8s %-20s has no data-desc" % (tag, ident))
        elif ident not in per_option:
            check_len(tag, ident, desc)

    for ident, descs in p.selects:
        for d in descs:
            if d and d.strip():
                check_len("option", ident, d.strip())

    # A select whose options differ in KIND shows the SELECTED option's text.
    # Describing some options and not others gives a hint that blanks out when
    # you pick the undescribed one — worse than describing none.
    for ident, descs in p.selects:
        if not descs:
            continue
        have = [d for d in descs if d]
        if have and len(have) != len(descs):
            bad.append("select   %-20s describes %d of %d options — the hint "
                       "goes blank on the rest" % (ident, len(have), len(descs)))

    if bad:
        print("FAIL: %d control(s) in the encode form are not described (#349)" % len(bad))
        for b in bad:
            print("  " + b)
        print("\nAdd data-desc=\"…\" to the control. Nothing else is needed —")
        print("initControlDescriptions() wires the hint, aria-describedby and")
        print("the demo recorder from that one attribute.")
        return 1

    # The wiring itself, pinned from the other end: the attribute is inert
    # without the function that reads it, and the function is what makes this
    # test's subject matter true.
    for needed in ("initControlDescriptions", "aria-describedby", "sr-only",
                   # Options carry a data-desc too, and they are read through
                   # their parent select. Walk them as controls and the wiring
                   # inserts a <div> after each <option>, i.e. inside a <select>:
                   # invalid, and silent — the hints still work.
                   "[data-desc]:not(option)"):
        if needed not in html:
            print("FAIL: %s is gone from the page — data-desc is inert without it"
                  % needed)
            return 1

    described = sum(1 for _, _, a in p.controls if (a.get("data-desc") or "").strip())
    opts = sum(len([d for d in ds if d]) for _, ds in p.selects)
    print("ok (%d control(s), %d option(s) described)" % (described, opts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
