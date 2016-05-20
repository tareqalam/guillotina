import logging

from plone.server.search.interfaces import ISearchUtility
from plone.uuid.interfaces import IUUID
from zope.component import getUtility
from zope.component import queryUtility
import transaction


logger = logging.getLogger('plone.server')


class CommitHook(object):

    def __init__(self):
        self.remove = []
        self.index = {}

    def __call__(self, trns):
        if not trns:
            return

        search = getUtility(ISearchUtility)
        search.remove(self.remove)
        search.index(self.index)

        self.index = {}
        self.remove = []


def get_hook():

    search = queryUtility(ISearchUtility)
    if not search:
        return  # no search configured

    trns = transaction.get()
    hook = None
    for _hook in trns._after_commit:
        if isinstance(_hook[0], CommitHook):
            hook = _hook[0]
            break
    if hook is None:
        hook = CommitHook()
        trns.addAfterCommitHook(hook)
    return hook


def remove_object(obj, event):
    hook = get_hook()
    if hook is None:
        return
    uid = IUUID(obj, None)
    if uid is None:
        return
    hook.remove.append(uid)
    if uid in hook.index:
        del hook.index[uid]


def add_object(obj, event):
    hook = get_hook()
    if hook is None:
        return
    uid = IUUID(obj, None)
    if uid is None:
        return
    search = queryUtility(ISearchUtility)
    hook.index[uid] = search.get_data(obj)