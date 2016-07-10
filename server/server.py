import hashlib
import json
import random
import string
import time
import os
import urllib.error
import urllib.parse
import urllib.request

from models import User, Session
from wson import WSON

from tornado import web, ioloop

MOTTO_LEN = 8


def rnd_gen(size=8, chars=string.ascii_lowercase + string.digits):
    return ''.join(random.choice(chars) for _ in range(size))


def get_god_info(name):
    try:
        raw = urllib.request.urlopen("http://godville.net/gods/api/" + urllib.parse.quote(name) + ".json").read()
    except urllib.error.HTTPError:
        return False
    return json.loads(raw.decode("utf-8"))


def check_users():
    global users
    rm = users[:]
    for c in clients:
        if c.user in rm: rm.remove(c.user)
    for u in rm:
        for c in clients:
            c.send("user", {"name": u.name, "status": "off"})
    users = [item for item in users if item not in rm]

clients = []
users = []
jump_time = 0
jump_delay = 300  # = (5 minutes) * 60


class ActiveUser:

    def __init__(self, name):
        self.name = name
        self.ready = False


class SocketHandler(WSON):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.auth_name = ""
        self.auth_salt = ""
        self.auth_sid = ""

        self.user = None

        self.on("sign", self.signup_ph1)
        self.on("salt", self.get_salt)
        self.on("auth", self.auth)
        self.on("sid", self.continue_session)
        self.on("test_auth", lambda: self.sendMessage(self.auth_name + ": " + str(self.user is not None)))

    def open(self):
        print(self.request.remote_ip, 'connected')
        clients.append(self)

    def on_close(self):
        print(self.request.remote_ip, 'closed')
        self.user = None
        check_users()
        clients.remove(self)

    def authorize(self, name):
        for u in users:
            if u.name == name:
                self.user = u
        if not self.user:
            u = ActiveUser(name)
            users.append(u)
            self.user = u
            for c in clients:
                c.send("user", {"name": u.name, "status": "on"})

        self.on("data", self.load_data)
        self.on("jump", self.jump)
        self.on("logout", self.close_session)

    # SIGN UP ##########

    def signup_ph1(self, data):
        god_info = get_god_info(data["login"])
        if not god_info:
            self.send_error_msg("Unknown god name")
            return
        self.auth_name = god_info['godname']

        user, created = User.get_or_create(god_name=self.auth_name)
        if created or user.motto_login is None or len(user.motto_login) < MOTTO_LEN:
            motto = rnd_gen(MOTTO_LEN)
            user.motto_login = motto
        else:
            motto = user.motto_login
        user.save()

        self.on("motto", self.signup_ph2)
        self.send("sign", {"motto": motto})

    def signup_ph2(self, _):
        user = User.get(god_name=self.auth_name)
        req_motto = user.motto_login

        god_info = get_god_info(self.auth_name)
        if not god_info:
            self.send_error_msg("Unknown god name")
            return
        motto = god_info['motto']

        if motto.find(req_motto) == -1:
            self.send("motto", {"status": "declined"})
        else:
            self.off("motto")
            self.on("password", self.signup_ph3)
            self.send("motto", {"status": "accepted"})

    def signup_ph3(self, data):
        password = data["password"]
        user = User.get(god_name=self.auth_name)
        user.password = hashlib.sha1(password.encode()).hexdigest()
        user.motto_login = ""
        user.save()

        self.off("password")
        self.send("password", {"status": "changed"})

    # AUTH ##########

    def get_salt(self, _):
        if len(self.auth_salt) < 32:
            self.auth_salt = rnd_gen(32)
        self.send("salt", {"salt": self.auth_salt})

    def auth(self, data):
        if len(self.auth_salt) < 32:
            return

        name = data["login"]

        try:
            user = User.get(god_name=name)
        except User.DoesNotExist:
            self.send("auth", {"status": "fail"})
            return

        req = hashlib.sha1((user.password + self.auth_salt).encode()).hexdigest()

        if not data["password"] == req:
            self.send("auth", {"status": "fail"})
            return

        self.auth_salt = ""
        self.auth_sid = rnd_gen(32)

        s = Session.create(sid=self.auth_sid, god=name)
        s.save()

        self.authorize(name)
        self.send("auth", {"status": "success", "sid": self.auth_sid})

    def continue_session(self, data):
        try:
            s = Session.get(sid=data["sid"])
        except Session.DoesNotExist:
            self.send("sid", {"status": "declined"})
            return

        self.auth_sid = s.sid
        self.authorize(s.god.god_name)
        self.send("sid", {"status": "accepted"})

    def close_session(self, _):
        try:
            s = Session.get(sid=self.auth_sid)
            s.delete_instance()
        except Session.DoesNotExist:
            pass
        self.send("logout", {})
        self.user = None
        check_users()
        self.off("data")
        self.off("jump")
        self.off("logout")

    # WORK ##########

    def load_data(self, _):
        gods = list(set([c.user.name for c in clients if c.user]))
        me = {
            "name": self.user.name,
            "ready": self.user.ready
        }
        jump_info = {"active": (jump_time - int(time.time())) > 0}
        if jump_info["active"]:
            jump_info["delay"] = jump_time - int(time.time())
            jump_info["ready"] = list(set([c.user.name for c in clients if c.user and c.user.ready]))
        self.send("data", {"users": gods, "me": me, "jump": jump_info})

    def jump(self, _):
        global jump_time

        if int(time.time()) < jump_time:  # join
            if not self.user.ready:
                for c in clients:
                    c.send("ready", {
                        "user": self.user.name
                    })
            self.user.ready = True
        else:  # start new
            jump_time = int(time.time()) + jump_delay

            for c in clients:
                c.user.ready = False
                c.send("jump", {
                    "delay": jump_time - int(time.time()),
                    "user": self.user.name
                })
            self.user.ready = True


class Page(web.RequestHandler):
    def get(self):
        self.render("index.html")


if __name__ == '__main__':
    settings = {
        "static_path": os.path.join(os.path.dirname(__file__), "static"),
        "debug": True,
    }

    app = web.Application([
        (r"/", Page),
        (r'/ws', SocketHandler),
        (r"/static/(.*)", web.StaticFileHandler, dict(path=settings['static_path'])),
    ])
    app.listen(8083)
    ioloop.IOLoop.instance().start()
