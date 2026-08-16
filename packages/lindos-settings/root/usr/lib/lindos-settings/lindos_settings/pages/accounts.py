"""Accounts page (delegate): Users and groups (users-admin / mintusers), Your info (mugshot).
Shows the signed-in user at the top."""

from __future__ import annotations

import os
from typing import Any

from .. import model
from ..widgets import Card, add_class, box, label
from . import DelegatePage


class AccountsPage(DelegatePage):
    PAGE_ID = "accounts"

    def build_extra_top(self) -> None:
        b = self.backend
        cards = self.add_section("")
        name = b.user_display_name()
        user = b.username()
        admin = self._is_admin()
        card = Card(name, f"{user} · {'Administrator' if admin else 'Standard user'} · {os.path.expanduser('~')}", None, ("user", "profile", "administrator"))
        avatar = Gtk_avatar(model.initials(name))
        card.header.pack_start(avatar, False, False, 0)
        card.header.reorder_child(avatar, 0)
        cards.add(card)
        self.add_widget(label("Change your name or picture with 'Your info'; add users or change passwords with 'Users and groups'.", ("dim-label",), wrap=True), 4)

    def _is_admin(self) -> bool:
        r = self.backend.run(["id", "-Gn"], timeout=5)
        groups = set(r.out.split()) if r.ok else set()
        return bool(groups & {"sudo", "admin", "wheel"})


def Gtk_avatar(text: str) -> Any:  # noqa: N802 — small factory
    from ..widgets import Gtk  # type: ignore

    lbl = Gtk.Label(label=text)
    lbl.set_size_request(40, 40)
    add_class(lbl, "avatar", "avatar-small")
    wrap = box("h", 0)
    wrap.pack_start(lbl, False, False, 0)
    wrap.set_valign(Gtk.Align.CENTER)
    return wrap


__all__: list[Any] = ["AccountsPage"]
