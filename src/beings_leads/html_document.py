"""Extraction-local HTML trees; destructive readers always receive private copies.

No cross-page/URL/time cache: exact markup and parser representation are the identity.
The owner drops all trees before returning. Different byte-limited representations stay
different, preserving each existing parser's input and interpretation.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

from bs4 import BeautifulSoup

_documents = ContextVar("html_document_trees", default=None)


def reuse_html(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _documents.get() is not None:
            return function(*args, **kwargs)
        token = _documents.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _documents.reset(token)

    return wrapped


def parse_html(markup, features="html.parser", *, mutable=False):
    documents = _documents.get()
    if documents is None:
        return BeautifulSoup(markup, features)
    key = markup, features
    if key not in documents:
        # A page can contain nested documents. Bound retention, never the evidence input.
        if len(documents) >= 4:
            return BeautifulSoup(markup, features)
        documents[key] = BeautifulSoup(markup, features)
    root = documents[key]
    return deepcopy(root) if mutable else root
