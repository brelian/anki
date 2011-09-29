# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import zlib, re, urllib, urllib2, socket, simplejson, time, shutil
import os, base64, sys, httplib2, types, zipfile
from cStringIO import StringIO
from datetime import date
from anki.db import DB
from anki.errors import *
from anki.utils import ids2str, checksum, intTime
from anki.consts import *
from anki.lang import _
from hooks import runHook

if simplejson.__version__ < "1.7.3":
    raise Exception("SimpleJSON must be 1.7.3 or later.")

CHUNK_SIZE = 32768
MIME_BOUNDARY = "Anki-sync-boundary"
SYNC_HOST = os.environ.get("SYNC_HOST") or "dev.ankiweb.net"
SYNC_PORT = int(os.environ.get("SYNC_PORT") or 80)
SYNC_URL = "http://%s:%d/sync/" % (SYNC_HOST, SYNC_PORT)

# fixme: status() should be using the hooks instead

# todo:
# - ensure all urllib references are converted to urllib2 for proxies
# - ability to cancel
# - need to make sure syncing doesn't bump the deck modified time if nothing was
#    changed, since by default closing the deck bumps the mod time
# - ensure the user doesn't add foreign chars to passsword
# - timeout on all requests (issue 2625)
# - ditch user/pass in favour of session key?

# full sync:
# - compress and divide into pieces
# - zlib? zip? content-encoding? if latter, need to account for bad proxies
#   that decompress.

##########################################################################

from anki.consts import *

class Syncer(object):

    MAX_REVLOG = 5000
    MAX_CARDS = 5000
    MAX_FACTS = 2500

    def __init__(self, deck, server=None):
        self.deck = deck
        self.server = server

    def status(self, type):
        "Override to trace sync progress."
        #print "sync:", type
        pass

    def sync(self):
        "Returns 'noChanges', 'fullSync', or 'success'."
        # step 1: login & metadata
        self.status("login")
        self.rmod, rscm, self.maxUsn = self.server.meta()
        self.lmod, lscm, self.minUsn = self.meta()
        if self.lmod == self.rmod:
            return "noChanges"
        elif lscm != rscm:
            return "fullSync"
        self.lnewer = self.lmod > self.rmod
        # step 2: deletions and small objects
        self.status("meta")
        lchg = self.changes()
        rchg = self.server.applyChanges(
            minUsn=self.minUsn, lnewer=self.lnewer, changes=lchg)
        self.mergeChanges(lchg, rchg)
        # step 3: stream large tables from server
        self.status("server")
        while 1:
            self.status("stream")
            chunk = self.server.chunk()
            self.applyChunk(chunk)
            if chunk['done']:
                break
        # step 4: stream to server
        self.status("client")
        while 1:
            self.status("stream")
            chunk = self.chunk()
            self.server.applyChunk(chunk)
            if chunk['done']:
                break
        # step 5: sanity check during beta testing
        self.status("sanity")
        c = self.sanityCheck()
        s = self.server.sanityCheck()
        assert c == s
        # finalize
        self.status("finalize")
        mod = self.server.finish()
        self.finish(mod)
        return "success"

    def meta(self):
        return (self.deck.mod, self.deck.scm, self.deck._usn)

    def changes(self):
        "Bundle up deletions and small objects, and apply if server."
        d = dict(models=self.getModels(),
                 groups=self.getGroups(),
                 tags=self.getTags(),
                 graves=self.getGraves())
        if self.lnewer:
            d['conf'] = self.getConf()
        return d

    def applyChanges(self, minUsn, lnewer, changes):
        # we're the server; save info
        self.maxUsn = self.deck._usn
        self.minUsn = minUsn
        self.lnewer = not lnewer
        self.rchg = changes
        lchg = self.changes()
        # merge our side before returning
        self.mergeChanges(lchg, self.rchg)
        return lchg

    def mergeChanges(self, lchg, rchg):
        # first, handle the deletions
        self.mergeGraves(rchg['graves'])
        # then the other objects
        self.mergeModels(rchg['models'])
        self.mergeGroups(rchg['groups'])
        self.mergeTags(rchg['tags'])
        if 'conf' in rchg:
            self.mergeConf(rchg['conf'])
        self.prepareToChunk()

    def sanityCheck(self):
        # some basic checks to ensure the sync went ok. this is slow, so will
        # be removed before official release
        assert not self.deck.db.scalar("""
select count() from cards where fid not in (select id from facts)""")
        assert not self.deck.db.scalar("""
select count() from facts where id not in (select distinct fid from cards)""")
        for t in "cards", "facts", "revlog", "graves":
            assert not self.deck.db.scalar(
                "select count() from %s where usn = -1" % t)
        for g in self.deck.groups.all():
            assert g['usn'] != -1
        for t, usn in self.deck.tags.allItems():
            assert usn != -1
        for m in self.deck.models.all():
            assert m['usn'] != -1
        return [
            self.deck.db.scalar("select count() from cards"),
            self.deck.db.scalar("select count() from facts"),
            self.deck.db.scalar("select count() from revlog"),
            self.deck.db.scalar("select count() from fsums"),
            self.deck.db.scalar("select count() from graves"),
            len(self.deck.models.all()),
            len(self.deck.tags.all()),
            len(self.deck.groups.all()),
            len(self.deck.groups.allConf()),
        ]

    def usnLim(self):
        if self.deck.server:
            return "usn >= %d" % self.minUsn
        else:
            return "usn = -1"

    def finish(self, mod=None):
        if not mod:
            # server side; we decide new mod time
            mod = intTime()
        self.deck.ls = mod
        self.deck._usn = self.maxUsn + 1
        self.deck.save(mod=mod)
        return mod

    # Chunked syncing
    ##########################################################################

    def prepareToChunk(self):
        self.tablesLeft = ["revlog", "cards", "facts"]
        self.cursor = None

    def cursorForTable(self, table):
        lim = self.usnLim()
        x = self.deck.db.execute
        d = (self.maxUsn, lim)
        if table == "revlog":
            return x("""
select id, cid, %d, ease, ivl, lastIvl, factor, time, type
from revlog where %s""" % d)
        elif table == "cards":
            return x("""
select id, fid, gid, ord, mod, %d, type, queue, due, ivl, factor, reps,
lapses, left, edue, flags, data from cards where %s""" % d)
        else:
            return x("""
select id, guid, mid, gid, mod, %d, tags, flds, '', flags, data
from facts where %s""" % d)

    def chunk(self):
        buf = dict(done=False)
        # gather up to 5000 records
        lim = 5000
        while self.tablesLeft and lim:
            curTable = self.tablesLeft[0]
            if not self.cursor:
                self.cursor = self.cursorForTable(curTable)
            rows = self.cursor.fetchmany(lim)
            fetched = len(rows)
            if fetched != lim:
                # table is empty
                self.tablesLeft.pop(0)
                self.cursor = None
                # if we're the client, mark the objects as having been sent
                if not self.deck.server:
                    self.deck.db.execute(
                        "update %s set usn=? where usn=-1"%curTable,
                        self.maxUsn)
            buf[curTable] = rows
            lim -= fetched
        if not self.tablesLeft:
            buf['done'] = True
        return buf

    def applyChunk(self, chunk):
        if "revlog" in chunk:
            self.mergeRevlog(chunk['revlog'])
        if "cards" in chunk:
            self.mergeCards(chunk['cards'])
        if "facts" in chunk:
            self.mergeFacts(chunk['facts'])

    # Deletions
    ##########################################################################

    def getGraves(self):
        cards = []
        facts = []
        groups = []
        if self.deck.server:
            curs = self.deck.db.execute(
                "select oid, type from graves where usn >= ?", self.minUsn)
        else:
            curs = self.deck.db.execute(
                "select oid, type from graves where usn = -1")
        for oid, type in curs:
            if type == REM_CARD:
                cards.append(oid)
            elif type == REM_FACT:
                facts.append(oid)
            else:
                groups.append(oid)
        if not self.deck.server:
            self.deck.db.execute("update graves set usn=? where usn=-1",
                                 self.maxUsn)
        return dict(cards=cards, facts=facts, groups=groups)

    def mergeGraves(self, graves):
        # facts first, so we don't end up with duplicate graves
        self.deck._remFacts(graves['facts'])
        self.deck.remCards(graves['cards'])
        for oid in graves['groups']:
            self.deck.groups.rem(oid)

    # Models
    ##########################################################################

    def getModels(self):
        if self.deck.server:
            return [m for m in self.deck.models.all() if m['usn'] >= self.minUsn]
        else:
            mods = [m for m in self.deck.models.all() if m['usn'] == -1]
            for m in mods:
                m['usn'] = self.maxUsn
            self.deck.models.save()
            return mods

    def mergeModels(self, rchg):
        for r in rchg:
            l = self.deck.models.get(r['id'])
            # if missing locally or server is newer, update
            if not l or r['mod'] > l['mod']:
                self.deck.models.update(r)

    # Groups
    ##########################################################################

    def getGroups(self):
        if self.deck.server:
            return [
                [g for g in self.deck.groups.all() if g['usn'] >= self.minUsn],
                [g for g in self.deck.groups.allConf() if g['usn'] >= self.minUsn]
            ]
        else:
            groups = [g for g in self.deck.groups.all() if g['usn'] == -1]
            for g in groups:
                g['usn'] = self.maxUsn
            gconf = [g for g in self.deck.groups.allConf() if g['usn'] == -1]
            for g in gconf:
                g['usn'] = self.maxUsn
            self.deck.groups.save()
            return [groups, gconf]

    def mergeGroups(self, rchg):
        for r in rchg[0]:
            l = self.deck.groups.get(r['id'], False)
            # if missing locally or server is newer, update
            if not l or r['mod'] > l['mod']:
                self.deck.groups.update(r)
        for r in rchg[1]:
            l = self.deck.groups.conf(r['id'])
            # if missing locally or server is newer, update
            if not l or r['mod'] > l['mod']:
                self.deck.groups.updateConf(r)

    # Tags
    ##########################################################################

    def getTags(self):
        if self.deck.server:
            return [t for t, usn in self.deck.tags.allItems()
                    if usn >= self.minUsn]
        else:
            tags = []
            for t, usn in self.deck.tags.allItems():
                if usn == -1:
                    self.deck.tags.tags[t] = self.maxUsn
                    tags.append(t)
            self.deck.tags.save()
            return tags

    def mergeTags(self, tags):
        self.deck.tags.register(tags, usn=self.maxUsn)

    # Cards/facts/revlog
    ##########################################################################

    def mergeRevlog(self, logs):
        self.deck.db.executemany(
            "insert or ignore into revlog values (?,?,?,?,?,?,?,?,?)",
            logs)

    def newerRows(self, data, table, modIdx):
        ids = (r[0] for r in data)
        lmods = {}
        for id, mod in self.deck.db.execute(
            "select id, mod from %s where id in %s and %s" % (
                table, ids2str(ids), self.usnLim())):
            lmods[id] = mod
        update = []
        for r in data:
            if r[0] not in lmods or lmods[r[0]] < r[modIdx]:
                update.append(r)
        return update

    def mergeCards(self, cards):
        self.deck.db.executemany(
            "insert or replace into cards values "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            self.newerRows(cards, "cards", 4))

    def mergeFacts(self, facts):
        rows = self.newerRows(facts, "facts", 4)
        self.deck.db.executemany(
            "insert or replace into facts values (?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        self.deck.updateFieldCache([f[0] for f in rows])

    # Deck config
    ##########################################################################

    def getConf(self):
        return self.deck.conf

    def mergeConf(self, conf):
        self.deck.conf = conf

class LocalServer(Syncer):
    # serialize/deserialize payload, so we don't end up sharing objects
    # between decks in testing
    def applyChanges(self, minUsn, lnewer, changes):
        l = simplejson.loads; d = simplejson.dumps
        return l(d(Syncer.applyChanges(self, minUsn, lnewer, l(d(changes)))))

class RemoteServer(Syncer):
    def __init__(self, user, hkey):
        self.user = user
        self.hkey = hkey

    def meta(self):
        h = httplib2.Http(timeout=60)
        resp, cont = h.request(
            SYNC_URL+"meta?" + urllib.urlencode(dict(u=self.user)))
        if resp['status'] != '200':
            raise Exception("Invalid response code: %s" % resp['status'])
        return simplejson.loads(cont)

    def hostKey(self, pw):
        h = httplib2.Http(timeout=60)
        resp, cont = h.request(
            SYNC_URL+"hostKey?" + urllib.urlencode(dict(u=self.user,p=pw)))
        if resp['status'] != '200':
            raise Exception("Invalid response code: %s" % resp['status'])
        self.hkey = cont
        return cont

    def _run(self, action, **args):
        data = {"p": self.password,
                "u": self.username,
                "v": 2}
        if self.deckName:
            data['d'] = self.deckName.encode("utf-8")
        else:
            data['d'] = None
        data.update(args)
        data = urllib.urlencode(data)
        try:
            f = urllib2.urlopen(SYNC_URL + action, data)
        except (urllib2.URLError, socket.error, socket.timeout,
                httplib.BadStatusLine), e:
            raise SyncError(type="connectionError",
                            exc=`e`)
        ret = f.read()
        if not ret:
            raise SyncError(type="noResponse")
        try:
            return self.unstuff(ret)
        except Exception, e:
            raise SyncError(type="connectionError",
                            exc=`e`)

    # def unstuff(self, data):
    #     return simplejson.loads(unicode(zlib.decompress(data), "utf8"))
    # def stuff(self, data):
    #     return zlib.compress(simplejson.dumps(data))

# HTTP proxy: act as a server and direct requests to the real server
##########################################################################
# not yet ported

class HttpSyncServerProxy(object):

    def __init__(self, user, passwd):
        SyncServer.__init__(self)
        self.decks = None
        self.deckName = None
        self.username = user
        self.password = passwd
        self.protocolVersion = 5
        self.sourcesToCheck = []

    def connect(self, clientVersion=""):
        "Check auth, protocol & grab deck list."
        if not self.decks:
            import socket
            socket.setdefaulttimeout(30)
            d = self.runCmd("getDecks",
                            libanki=anki.version,
                            client=clientVersion,
                            sources=simplejson.dumps(self.sourcesToCheck),
                            pversion=self.protocolVersion)
            socket.setdefaulttimeout(None)
            if d['status'] != "OK":
                raise SyncError(type="authFailed", status=d['status'])
            self.decks = d['decks']
            self.timestamp = d['timestamp']
            self.timediff = abs(self.timestamp - time.time())

    def hasDeck(self, deckName):
        self.connect()
        return deckName in self.decks.keys()

    def availableDecks(self):
        self.connect()
        return self.decks.keys()

    def createDeck(self, deckName):
        ret = self.runCmd("createDeck", name=deckName.encode("utf-8"))
        if not ret or ret['status'] != "OK":
            raise SyncError(type="createFailed")
        self.decks[deckName] = [0, 0]

    def summary(self, lastSync):
        return self.runCmd("summary",
                           lastSync=self.stuff(lastSync))

    def genOneWayPayload(self, lastSync):
        return self.runCmd("genOneWayPayload",
                           lastSync=self.stuff(lastSync))

    def modified(self):
        self.connect()
        return self.decks[self.deckName][0]

    def _lastSync(self):
        self.connect()
        return self.decks[self.deckName][1]

    def applyPayload(self, payload):
        return self.runCmd("applyPayload",
                           payload=self.stuff(payload))

    def finish(self):
        assert self.runCmd("finish") == "OK"

# Full syncing
##########################################################################
# not yet ported

# make sure it resets any usn == -1 before uploading!
# make sure webserver is sending gzipped

class FullSyncer(object):

    def __init__(self, deck, hkey):
        self.deck = deck
        self.hkey = hkey

    def download(self):
        self.deck.close()
        h = httplib2.Http(timeout=60)
        resp, cont = h.request(
            SYNC_URL+"download?" + urllib.urlencode(dict(k=self.hkey)))
        if resp['status'] != '200':
            raise Exception("Invalid response code: %s" % resp['status'])
        tpath = self.deck.path + ".tmp"
        open(tpath, "wb").write(cont)
        os.unlink(self.deck.path)
        os.rename(tpath, self.deck.path)
        d = DB(self.deck.path)
        assert d.scalar("pragma integrity_check") == "ok"
        self.deck = None

    def upload(self):
        self.deck.beforeUpload()
        # compressed post body support is flaky, so bundle into a zip
        f = StringIO()
        z = zipfile.ZipFile(f, mode="w", compression=zipfile.ZIP_DEFLATED)
        z.write(self.deck.path, "col.anki")
        z.close()
        # build an upload body
        f2 = StringIO()
        fields = dict(k=self.hkey)
        # post vars
        for (key, value) in fields.items():
            f2.write('--' + MIME_BOUNDARY + "\r\n")
            f2.write('Content-Disposition: form-data; name="%s"\r\n' % key)
            f2.write('\r\n')
            f2.write(value)
            f2.write('\r\n')
        # file header
        f2.write('--' + MIME_BOUNDARY + "\r\n")
        f2.write(
            'Content-Disposition: form-data; name="deck"; filename="deck"\r\n')
        f2.write('Content-Type: application/octet-stream\r\n')
        f2.write('\r\n')
        f2.write(f.getvalue())
        f.close()
        f2.write('\r\n--' + MIME_BOUNDARY + '--\r\n\r\n')
        size = f2.tell()
        # connection headers
        headers = {
            'Content-type': 'multipart/form-data; boundary=%s' %
            MIME_BOUNDARY,
            'Content-length': str(size),
            'Host': SYNC_HOST,
        }
        body = f2.getvalue()
        f2.close()
        h = httplib2.Http(timeout=60)
        resp, cont = h.request(
            SYNC_URL+"upload", "POST", headers=headers, body=body)
        if resp['status'] != '200':
            raise Exception("Invalid response code: %s" % resp['status'])
        assert cont == "OK"
