import os, uuid, json, logging
from functools import wraps
import boto3, pymysql
from botocore.config import Config
from flask import Flask, request, redirect, session, flash, render_template_string
from werkzeug.security import generate_password_hash, check_password_hash

logging.basicConfig(level=logging.INFO)
REGION = os.environ.get("AWS_REGION", "ap-south-1")
ssm = boto3.client("ssm", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
sns = boto3.client("sns", region_name=REGION)


def param(name):  # secrets come from SSM Parameter Store or env vars, never hard-coded
    env_val = os.environ.get("LOSTFOUND_" + name.upper())
    if env_val:
        return env_val
    try:
        return ssm.get_parameter(Name="/lostfound/" + name, WithDecryption=True)["Parameter"]["Value"]
    except Exception as e:
        logging.warning("SSM parameter /lostfound/%s unavailable: %s. Using demo fallback.", name, e)
        fallbacks = {
            "db_host": "localhost",
            "db_user": "root",
            "db_pass": "password",
            "db_name": "lostfound",
            "bucket": "lostfound-demo-bucket",
            "topic_arn": "arn:aws:sns:demo:000000000000:lostfound-matches",
            "secret_key": "campus-lost-found-local-secret-key",
        }
        return fallbacks.get(name, "")


C = {k: param(k) for k in ["db_host", "db_user", "db_pass", "db_name", "bucket", "topic_arn", "secret_key"]}
app = Flask(__name__)
app.secret_key = C["secret_key"]
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
CATS = ["ID card", "Bottle", "Electronics", "Books", "Bag", "Keys", "Other"]


def q(sql, args=(), one=False):
    con = pymysql.connect(host=C["db_host"], user=C["db_user"], password=C["db_pass"],
                          database=C["db_name"], cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    try:
        with con.cursor() as cur:
            cur.execute(sql, args)
            if sql.lstrip().upper().startswith("SELECT"):
                return cur.fetchone() if one else cur.fetchall()
            return cur.lastrowid
    finally:
        con.close()


def init_db():
    q("""CREATE TABLE IF NOT EXISTS users (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100),
         email VARCHAR(120) UNIQUE, roll_no VARCHAR(20), password_hash VARCHAR(255))""")
    q("""CREATE TABLE IF NOT EXISTS items (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT,
         type ENUM('lost','found'), title VARCHAR(150), category VARCHAR(50), location VARCHAR(100),
         event_date DATE, description TEXT, image_key VARCHAR(255),
         status ENUM('open','returned') DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    q("""CREATE TABLE IF NOT EXISTS matches (id INT AUTO_INCREMENT PRIMARY KEY, lost_id INT, found_id INT,
         score INT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")


def presign(key):  # private bucket -> short-lived presigned URL
    if not key:
        return None
    try:
        return s3.generate_presigned_url("get_object", Params={"Bucket": C["bucket"], "Key": key}, ExpiresIn=600)
    except Exception as e:
        logging.warning("Presign URL failed for key %s: %s", key, e)
        return None


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("uid"):
            return redirect("/login")
        return f(*a, **k)
    return w


LAYOUT = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>CampusLost&Found</title>
<link rel=preconnect href="https://fonts.googleapis.com"><link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel=stylesheet>
<style>
:root{--ink:#14213d;--muted:#5b6578;--bg:#eef2f7;--card:#fff;--lost:#d1495b;--found:#00798c;--line:#d5dce7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
h1,h2,h3,.brand{font-family:'Bricolage Grotesque',Inter,system-ui,sans-serif;letter-spacing:-.02em}
a{color:inherit}
a:focus-visible,button:focus-visible{outline:3px solid var(--found);outline-offset:2px}
.wrap{max-width:980px;margin:0 auto;padding:0 1.25rem}
header{background:var(--ink);color:#fff}
header .wrap{display:flex;align-items:center;gap:1.1rem;flex-wrap:wrap;padding-top:.85rem;padding-bottom:.85rem}
.brand{font-size:1.3rem;font-weight:700;text-decoration:none;margin-right:auto}
header nav{display:flex;gap:1.1rem;flex-wrap:wrap}
header nav a{text-decoration:none;opacity:.8}
header nav a:hover{opacity:1}
main{padding:1.5rem 0 3rem}
.hero{padding:1.5rem 0 .5rem}
.hero h1{font-size:clamp(2.1rem,6vw,3.4rem);line-height:1.04;margin:0 0 .7rem;max-width:15ch}
.hero p{color:var(--muted);max-width:48ch;margin:0 0 1.3rem}
.actions{display:flex;gap:.7rem;flex-wrap:wrap}
.btn{display:inline-block;border:0;border-radius:10px;padding:.7rem 1.2rem;font:600 1rem Inter,sans-serif;cursor:pointer;text-decoration:none;color:#fff;background:var(--ink)}
.btn.lost{background:var(--lost)}.btn.found{background:var(--found)}
.btn.small{padding:.4rem .8rem;font-size:.9rem}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.flash{background:#dff3f5;border-left:4px solid var(--found);padding:.65rem .9rem;border-radius:6px;margin:0 0 1rem}
.filters{display:flex;gap:.6rem;flex-wrap:wrap;margin:1.6rem 0 1rem;align-items:center}
.filters select{width:auto;margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1rem}
.ticket{display:flex;background:var(--card);border-radius:14px;border-left:8px solid var(--tc);text-decoration:none;min-height:112px;box-shadow:0 1px 3px rgba(20,33,61,.1)}
.ticket.lost{--tc:var(--lost)}.ticket.found{--tc:var(--found)}
.ticket:hover{box-shadow:0 4px 14px rgba(20,33,61,.16)}
.thumb{position:relative;width:96px;flex:none;background:#e3e9f1;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-size:.8rem;padding:.3rem}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.thumb::before,.thumb::after{content:"";position:absolute;right:-8px;width:16px;height:16px;border-radius:50%;background:var(--bg);z-index:2}
.thumb::before{top:-8px}.thumb::after{bottom:-8px}
.tbody{padding:.8rem 1rem .8rem 1.2rem;border-left:2px dashed var(--line);min-width:0}
.tbody h3{margin:.1rem 0 .2rem;font-size:1.1rem;line-height:1.25;overflow-wrap:anywhere}
.tag{font-weight:600;font-size:.85rem;color:var(--tc)}
.meta{color:var(--muted);font-size:.9rem;margin:0}
.empty{grid-column:1/-1;background:var(--card);padding:2rem;border-radius:14px;text-align:center;color:var(--muted)}
.panel{background:var(--card);border-radius:14px;padding:1.5rem;max-width:520px;box-shadow:0 1px 3px rgba(20,33,61,.1)}
.panel.wide{max-width:none}
.panel h2{margin:0 0 1rem}
label{display:block;font-weight:500;font-size:.9rem;margin-top:.8rem}
input,select,textarea{width:100%;padding:.65rem .75rem;margin:.3rem 0;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff;color:var(--ink)}
input:focus,select:focus,textarea:focus{outline:2px solid var(--found);outline-offset:1px}
.panel .btn{margin-top:1rem}
.sub{color:var(--muted);font-size:.9rem;margin-top:1rem}
.detail{display:grid;grid-template-columns:minmax(0,1fr);gap:1.2rem}
@media(min-width:760px){.detail.has-img{grid-template-columns:340px 1fr}}
.detail img{width:100%;border-radius:14px;display:block}
.detail h1{margin:.2rem 0 .4rem;font-size:2rem;line-height:1.15}
.contact{margin-top:1rem;padding:.9rem 1rem;border-radius:10px;background:#e8eef6}
h2.sec{margin:2rem 0 .8rem}
.match{background:var(--card);border-radius:14px;padding:.9rem 1.1rem;margin-bottom:.7rem;display:flex;gap:.8rem;flex-wrap:wrap;align-items:center;box-shadow:0 1px 3px rgba(20,33,61,.1)}
.match .score{margin-left:auto;font-weight:600;color:var(--found)}
.row{background:var(--card);border-radius:12px;padding:.75rem 1rem;margin-bottom:.6rem;display:flex;gap:.8rem;align-items:center;flex-wrap:wrap}
.row .grow{flex:1;min-width:160px}
.chip{border-radius:99px;padding:.1rem .65rem;font-size:.82rem;font-weight:600;background:#e8eef6}
.chip.returned{background:#dcf3e6;color:#1d6b3f}
footer{color:var(--muted);font-size:.85rem;text-align:center;padding:1.5rem 0 2rem}
</style></head><body>
<header><div class=wrap><a class=brand href="/">CampusLost&amp;Found</a><nav>
<a href="/">Browse</a><a href="/post">Post an item</a><a href="/my">My items</a>
{% if session.uid %}<a href="/logout">Log out ({{ session.name }})</a>{% else %}<a href="/login">Log in</a><a href="/register">Register</a>{% endif %}
</nav></div></header>
<main class=wrap>
{% for m in get_flashed_messages() %}<div class=flash>{{ m }}</div>{% endfor %}
"""
FOOT = "</main><footer>CampusLost&amp;Found &middot; hosted on AWS</footer></body></html>"


def page(body, **ctx):
    return render_template_string(LAYOUT + body + FOOT, **ctx)


@app.route("/health")
def health():
    return "ok"


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        if q("SELECT id FROM users WHERE email=%s", (f["email"],), one=True):
            flash("That email is already registered. Try logging in.")
        else:
            q("INSERT INTO users(name,email,roll_no,password_hash) VALUES(%s,%s,%s,%s)",
              (f["name"], f["email"], f["roll_no"], generate_password_hash(f["password"])))
            flash("Account created. Log in to continue.")
            return redirect("/login")
    return page("""<div class=panel><h2>Create your account</h2><form method=post>
<label for=n>Full name</label><input id=n name=name required>
<label for=e>College email</label><input id=e name=email type=email required>
<label for=r>Roll number</label><input id=r name=roll_no required>
<label for=p>Password</label><input id=p name=password type=password required>
<button class="btn">Create account</button></form>
<p class=sub>Already registered? <a href="/login">Log in</a></p></div>""")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE email=%s", (request.form["email"],), one=True)
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session["uid"], session["name"] = u["id"], u["name"]
            return redirect("/")
        flash("Email or password is incorrect.")
    return page("""<div class=panel><h2>Log in</h2><form method=post>
<label for=e>Email</label><input id=e name=email type=email required>
<label for=p>Password</label><input id=p name=password type=password required>
<button class="btn">Log in</button></form>
<p class=sub>New here? <a href="/register">Create an account</a></p></div>""")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/")
def home():
    t, cat = request.args.get("type", ""), request.args.get("category", "")
    sql, args = "SELECT * FROM items WHERE status='open'", []
    if t:
        sql += " AND type=%s"; args.append(t)
    if cat:
        sql += " AND category=%s"; args.append(cat)
    items = q(sql + " ORDER BY id DESC", args)
    for i in items:
        i["thumb"] = presign(i["image_key"])
    return page("""<section class=hero><h1>Lost something on campus?</h1>
<p>Post what you lost or found. We compare new posts with open ones and email you when a likely match appears.</p>
<div class=actions><a class="btn lost" href="/post?type=lost">I lost something</a>
<a class="btn found" href="/post?type=found">I found something</a></div></section>
<form method=get class=filters>
<select name=type aria-label="Type"><option value="">Lost and found</option>
<option value=lost {{ 'selected' if t=='lost' }}>Lost only</option><option value=found {{ 'selected' if t=='found' }}>Found only</option></select>
<select name=category aria-label="Category"><option value="">All categories</option>
{% for c in cats %}<option {{ 'selected' if c==cat }}>{{ c }}</option>{% endfor %}</select>
<button class="btn small ghost">Apply filter</button></form>
<div class=grid>{% for i in items %}
<a class="ticket {{ i.type }}" href="/item/{{ i.id }}">
<div class=thumb>{% if i.thumb %}<img src="{{ i.thumb }}" alt="">{% else %}{{ i.category }}{% endif %}</div>
<div class=tbody><span class=tag>{{ 'Lost' if i.type=='lost' else 'Found' }}</span><h3>{{ i.title }}</h3>
<p class=meta>{{ i.location }}, {{ i.event_date.strftime('%d %b %Y') }}</p></div></a>
{% else %}<div class=empty>No open reports match this filter yet. Post the first one.</div>{% endfor %}</div>""",
                items=items, cats=CATS, t=t, cat=cat)


def score(a, b):
    s = 0
    if a["category"] == b["category"]: s += 40
    if a["location"].strip().lower() == b["location"].strip().lower(): s += 25
    if abs((a["event_date"] - b["event_date"]).days) <= 3: s += 15
    s += min(20, 5 * len(set(a["title"].lower().split()) & set(b["title"].lower().split())))
    return s


def run_matching(iid):
    it = q("SELECT * FROM items WHERE id=%s", (iid,), one=True)
    other = "found" if it["type"] == "lost" else "lost"
    for c in q("SELECT * FROM items WHERE type=%s AND status='open' AND user_id<>%s", (other, it["user_id"])):
        s = score(it, c)
        if s >= 60:
            lost, found = (it, c) if it["type"] == "lost" else (c, it)
            q("INSERT INTO matches(lost_id,found_id,score) VALUES(%s,%s,%s)", (lost["id"], found["id"], s))
            try:
                sns.publish(TopicArn=C["topic_arn"], Subject="Possible match on CampusLost&Found",
                            Message=json.dumps({"lost_id": lost["id"], "lost_title": lost["title"],
                                                "found_id": found["id"], "found_title": found["title"], "score": s}))
            except Exception as e:
                logging.warning("SNS publish failed: %s", e)
            logging.info("match lost=%s found=%s score=%s", lost["id"], found["id"], s)


@app.route("/post", methods=["GET", "POST"])
@login_required
def post():
    if request.method == "POST":
        f, key = request.form, None
        img = request.files.get("image")
        if img and img.filename:
            ext = img.filename.rsplit(".", 1)[-1].lower()
            if ext not in ("jpg", "jpeg", "png"):
                flash("Photos must be .jpg or .png files.")
                return redirect("/post")
            key = "items/%s.%s" % (uuid.uuid4().hex, ext)
            try:
                s3.upload_fileobj(img, C["bucket"], key)
            except Exception as e:
                logging.warning("S3 upload failed: %s", e)
        iid = q("""INSERT INTO items(user_id,type,title,category,location,event_date,description,image_key)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (session["uid"], f["type"], f["title"], f["category"], f["location"],
                 f["event_date"], f["description"], key))
        run_matching(iid)
        flash("Your report is posted. We will email you if a match turns up.")
        return redirect("/")
    return page("""<div class=panel><h2>Report an item</h2><form method=post enctype=multipart/form-data>
<label for=t>What happened?</label><select id=t name=type>
<option value=lost {{ 'selected' if t=='lost' }}>I lost it</option><option value=found {{ 'selected' if t=='found' }}>I found it</option></select>
<label for=ti>Item name</label><input id=ti name=title placeholder="Blue steel water bottle" required>
<label for=c>Category</label><select id=c name=category>{% for c in cats %}<option>{{ c }}</option>{% endfor %}</select>
<label for=l>Where (building or area)</label><input id=l name=location placeholder="Library" required>
<label for=d>Date</label><input id=d name=event_date type=date required>
<label for=ds>Details</label><textarea id=ds name=description rows=3 placeholder="Colour, brand, marks, anything that identifies it"></textarea>
<label for=im>Photo (optional, jpg or png)</label><input id=im type=file name=image accept="image/*">
<button class="btn">Post report</button></form></div>""",
                cats=CATS, t=request.args.get("type", "lost"))


@app.route("/item/<int:iid>")
def item(iid):
    i = q("""SELECT i.*, u.name AS poster, u.email AS poster_email FROM items i
             JOIN users u ON u.id=i.user_id WHERE i.id=%s""", (iid,), one=True)
    if not i:
        return "Not found", 404
    return page("""<p><a href="/">&larr; Back to all reports</a></p>
<div class="detail {{ 'has-img' if url }}">{% if url %}<img src="{{ url }}" alt="Photo of {{ i.title }}">{% endif %}
<div><span class=tag style="color:{{ 'var(--lost)' if i.type=='lost' else 'var(--found)' }}">{{ 'Lost' if i.type=='lost' else 'Found' }}</span>
<h1>{{ i.title }}</h1><p class=meta>{{ i.category }} &middot; {{ i.location }} &middot; {{ i.event_date.strftime('%d %b %Y') }}</p>
<p>{{ i.description or 'No extra details provided.' }}</p>
{% if session.uid %}<div class=contact>Contact {{ i.poster }}: <a href="mailto:{{ i.poster_email }}">{{ i.poster_email }}</a></div>
{% else %}<div class=contact><a href="/login">Log in</a> to see how to contact the person who posted this.</div>{% endif %}
</div></div>""", i=i, url=presign(i["image_key"]))


@app.route("/my")
@login_required
def my():
    uid = session["uid"]
    items = q("SELECT * FROM items WHERE user_id=%s ORDER BY id DESC", (uid,))
    matches = q("""SELECT m.score, l.id lost_id, l.title lost_title, f.id found_id, f.title found_title
                   FROM matches m JOIN items l ON l.id=m.lost_id JOIN items f ON f.id=m.found_id
                   WHERE l.user_id=%s OR f.user_id=%s""", (uid, uid))
    return page("""<h2 class=sec>Possible matches</h2>
{% for m in matches %}<div class=match><span><b>Lost:</b> <a href="/item/{{ m.lost_id }}">{{ m.lost_title }}</a></span>
<span><b>Found:</b> <a href="/item/{{ m.found_id }}">{{ m.found_title }}</a></span><span class=score>Match score {{ m.score }}</span></div>
{% else %}<p class=meta>No matches yet. We email you as soon as one appears.</p>{% endfor %}
<h2 class=sec>Your reports</h2>
{% for i in items %}<div class=row><span class=grow><b>{{ i.title }}</b><br><span class=meta>{{ 'Lost' if i.type=='lost' else 'Found' }} &middot; {{ i.location }}</span></span>
<span class="chip {{ i.status }}">{{ 'Returned' if i.status=='returned' else 'Open' }}</span>
{% if i.status=='open' %}<form method=post action="/returned/{{ i.id }}"><button class="btn small ghost">Mark as returned</button></form>{% endif %}</div>
{% else %}<p class=meta>You have not posted anything yet.</p>{% endfor %}""", items=items, matches=matches)


@app.route("/returned/<int:iid>", methods=["POST"])
@login_required
def returned(iid):
    q("UPDATE items SET status='returned' WHERE id=%s AND user_id=%s", (iid, session["uid"]))
    flash("Marked as returned. Glad it found its way back.")
    return redirect("/my")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=80)
