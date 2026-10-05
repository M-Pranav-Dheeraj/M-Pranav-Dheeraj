import io,json,os,re,hashlib,hmac,secrets
import httpx
from datetime import datetime,timezone
from pathlib import Path
from fastapi import FastAPI,File,Form,Request,UploadFile,HTTPException
from fastapi.responses import HTMLResponse,RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pypdf import PdfReader
from docx import Document
from sqlalchemy import create_engine,text
from .matcher import match_job

BASE=Path(__file__).resolve().parent
UPLOADS=BASE.parent/"uploads"; UPLOADS.mkdir(exist_ok=True)
templates=Jinja2Templates(directory=str(BASE/"templates"))
URL=os.getenv("DATABASE_URL","")
if URL.startswith("postgres://"): URL=URL.replace("postgres://","postgresql+psycopg://",1)
elif URL.startswith("postgresql://"): URL=URL.replace("postgresql://","postgresql+psycopg://",1)
engine=create_engine(URL or "sqlite:///"+str(BASE.parent/"jobpilot.db"),connect_args={"check_same_thread":False} if not URL else {},pool_pre_ping=True)
app=FastAPI(title="JobPilot AI",version="0.2.0")
app.add_middleware(SessionMiddleware,secret_key=os.getenv("SESSION_SECRET","dev-change-me"),same_site="lax",https_only=os.getenv("ENVIRONMENT")=="production")

ROLES="Data Engineer, Data Analyst, AI/ML Engineer, Software Engineer, Python Developer"
LOCS="India, Hyderabad, Bangalore, Pune, Chennai, Remote"
SKILLS="Python, SQL, Snowflake, AWS, Azure, Machine Learning, Pandas, NumPy, TensorFlow, Scikit-learn, Power BI, C++, C#/.NET"

def sql(s,p=None,one=False):
    with engine.begin() as c:
        r=c.execute(text(s),p or {})
        a=[dict(x._mapping) for x in r]
        return a[0] if one and a else (None if one else a)
def run(s,p=None):
    with engine.begin() as c:return c.execute(text(s),p or {})
def init():
    with engine.begin() as c:
        if URL:
            c.execute(text("CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)"))
            c.execute(text("CREATE TABLE IF NOT EXISTS profiles(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,name TEXT DEFAULT '',phone TEXT DEFAULT '',roles TEXT DEFAULT '',locations TEXT DEFAULT '',skills TEXT DEFAULT '',min_score INTEGER DEFAULT 70,mode TEXT DEFAULT 'smart',experience TEXT DEFAULT '',work_mode TEXT DEFAULT 'Any',min_salary TEXT DEFAULT '',auto_apply_enabled BOOLEAN DEFAULT FALSE,daily_runs INTEGER DEFAULT 4,resume_text TEXT DEFAULT '')"))
            c.execute(text("CREATE TABLE IF NOT EXISTS user_settings(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,theme TEXT DEFAULT 'light',updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)"))
            c.execute(text("CREATE TABLE IF NOT EXISTS resumes(id SERIAL PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,filename TEXT,extracted_text TEXT,uploaded_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)"))
            c.execute(text("CREATE TABLE IF NOT EXISTS jobs(id SERIAL PRIMARY KEY,source TEXT NOT NULL,external_id TEXT NOT NULL,title TEXT,company TEXT,location TEXT,url TEXT,description TEXT,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,UNIQUE(source,external_id))"))
            c.execute(text("CREATE TABLE IF NOT EXISTS job_matches(user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,match_score REAL,matched_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,job_id))"))
            c.execute(text("CREATE TABLE IF NOT EXISTS applications(id SERIAL PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,status TEXT DEFAULT 'prepared',answers TEXT,notes TEXT,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,UNIQUE(user_id,job_id))"))
        else:
            c.execute(text("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP)"))
            c.execute(text("CREATE TABLE IF NOT EXISTS profiles(user_id INTEGER PRIMARY KEY,name TEXT DEFAULT '',phone TEXT DEFAULT '',roles TEXT DEFAULT '',locations TEXT DEFAULT '',skills TEXT DEFAULT '',min_score INTEGER DEFAULT 70,mode TEXT DEFAULT 'smart',experience TEXT DEFAULT '',work_mode TEXT DEFAULT 'Any',min_salary TEXT DEFAULT '',auto_apply_enabled INTEGER DEFAULT 0,daily_runs INTEGER DEFAULT 4,resume_text TEXT DEFAULT '')"))
            c.execute(text("CREATE TABLE IF NOT EXISTS user_settings(user_id INTEGER PRIMARY KEY,theme TEXT DEFAULT 'light',updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"))
            c.execute(text("CREATE TABLE IF NOT EXISTS resumes(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,filename TEXT,extracted_text TEXT,uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP)"))
            c.execute(text("CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL,external_id TEXT NOT NULL,title TEXT,company TEXT,location TEXT,url TEXT,description TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(source,external_id))"))
            c.execute(text("CREATE TABLE IF NOT EXISTS job_matches(user_id INTEGER,job_id INTEGER,match_score REAL,matched_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,job_id))"))
            c.execute(text("CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,job_id INTEGER,status TEXT DEFAULT 'prepared',answers TEXT,notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(user_id,job_id))"))
init()

def ensure_apply_workflow_tables():
    with engine.begin() as c:
        if URL:
            c.execute(text("CREATE TABLE IF NOT EXISTS application_events(id SERIAL PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,status TEXT NOT NULL,event_type TEXT NOT NULL,detail TEXT DEFAULT '',requires_approval BOOLEAN DEFAULT FALSE,approved BOOLEAN DEFAULT FALSE,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)"))
        else:
            c.execute(text("CREATE TABLE IF NOT EXISTS application_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,job_id INTEGER,status TEXT NOT NULL,event_type TEXT NOT NULL,detail TEXT DEFAULT '',requires_approval INTEGER DEFAULT 0,approved INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP)"))
ensure_apply_workflow_tables()

def ensure_matching_columns():
    # Portable migrations for existing SQLite/Postgres databases.
    statements=[
        "ALTER TABLE jobs ADD COLUMN posted_at TEXT",
        "ALTER TABLE jobs ADD COLUMN last_seen_at TEXT",
        "ALTER TABLE job_matches ADD COLUMN ai_score REAL",
        "ALTER TABLE job_matches ADD COLUMN matched_skills TEXT DEFAULT ''",
        "ALTER TABLE job_matches ADD COLUMN missing_skills TEXT DEFAULT ''",
        "ALTER TABLE job_matches ADD COLUMN match_reasons TEXT DEFAULT ''",
    ]
    with engine.begin() as c:
        for stmt in statements:
            try:
                c.execute(text(stmt))
            except Exception:
                pass
ensure_matching_columns()

def event(u,j,status,event_type,detail="",approval=False):
    run("INSERT INTO application_events(user_id,job_id,status,event_type,detail,requires_approval,approved) VALUES(:u,:j,:s,:e,:d,:r,0)",{"u":u,"j":j,"s":status,"e":event_type,"d":detail,"r":1 if approval else 0})

def blocker_type(value):
    s=(value or "").lower()
    for kind,terms in [("captcha",("captcha","recaptcha","hcaptcha")),("mfa",("multi-factor","multifactor","mfa","verification code","one-time password","otp")),("assessment",("assessment","coding test","technical test","skills test")),("legal_declaration",("legal declaration","attest","certify","i certify","truthfulness declaration"))]:
        if any(t in s for t in terms): return kind
    return None

def ph(p):
    s=secrets.token_bytes(16); k=hashlib.scrypt(p.encode(),salt=s,n=2**14,r=8,p=1); return "scrypt$"+s.hex()+"$"+k.hex()
def pv(p,e):
    try:
        _,s,k=e.split("$"); x=hashlib.scrypt(p.encode(),salt=bytes.fromhex(s),n=2**14,r=8,p=1); return hmac.compare_digest(x.hex(),k)
    except:return False
def user(r):
    uid=r.session.get("uid"); return sql("SELECT * FROM users WHERE id=:id",{"id":uid},True) if uid else None
def need(r):
    u=user(r)
    if not u: raise HTTPException(401,"Login required")
    return u
def toks(s):return set(re.findall(r"[a-zA-Z0-9+#.]{2,}",(s or "").lower()))

def score(j,p):
    return match_job(j,p)["score"]

def resume_text(data,name):
    if name.lower().endswith(".pdf"):return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    if name.lower().endswith(".docx"):return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    raise ValueError("Upload PDF or DOCX")
def page(r,n,**x):return templates.TemplateResponse(request=r,name=n,context=x)

@app.get("/login",response_class=HTMLResponse)
def lp(r:Request):return page(r,"auth.html",mode="login",error=None)
@app.post("/login")
def li(r:Request,email:str=Form(...),password:str=Form(...)):
    u=sql("SELECT * FROM users WHERE email=:e",{"e":email.strip().lower()},True)
    if not u or not pv(password,u["password_hash"]):return page(r,"auth.html",mode="login",error="Invalid email or password.")
    r.session["uid"]=u["id"];return RedirectResponse("/",303)
@app.get("/register",response_class=HTMLResponse)
def rp(r:Request):return page(r,"auth.html",mode="register",error=None)
@app.post("/register")
def reg(r:Request,email:str=Form(...),password:str=Form(...),name:str=Form("")):
    email=email.strip().lower()
    if len(password)<8:return page(r,"auth.html",mode="register",error="Password must be at least 8 characters.")
    if sql("SELECT id FROM users WHERE email=:e",{"e":email},True):return page(r,"auth.html",mode="register",error="Email already registered.")
    run("INSERT INTO users(email,password_hash) VALUES(:e,:p)",{"e":email,"p":ph(password)})
    uid=sql("SELECT id FROM users WHERE email=:e",{"e":email},True)["id"]
    run("INSERT INTO profiles(user_id,name,roles,locations,skills,daily_runs) VALUES(:u,:n,:r,:l,:s,4)",{"u":uid,"n":name,"r":ROLES,"l":LOCS,"s":SKILLS})
    run("INSERT INTO user_settings(user_id,theme) VALUES(:u,'light') ON CONFLICT(user_id) DO NOTHING",{"u":uid})
    r.session["uid"]=uid;return RedirectResponse("/",303)
@app.post("/logout")
def logout(r:Request):r.session.clear();return RedirectResponse("/login",303)

@app.get("/",response_class=HTMLResponse)
def home(r:Request):
    u=user(r)
    if not u:return RedirectResponse("/login",303)
    p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    jobs=sql("SELECT j.*,COALESCE(m.ai_score,m.match_score,0) score,COALESCE(m.matched_skills,'') matched_skills,COALESCE(m.missing_skills,'') missing_skills,COALESCE(m.match_reasons,'') match_reasons FROM jobs j LEFT JOIN job_matches m ON m.job_id=j.id AND m.user_id=:u ORDER BY score DESC,j.id DESC LIMIT 100",{"u":u["id"]})
    apps=sql("SELECT a.*,j.title,j.company FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.user_id=:u ORDER BY a.updated_at DESC LIMIT 100",{"u":u["id"]})
    approvals=sql("SELECT e.*,j.title,j.company FROM application_events e JOIN jobs j ON j.id=e.job_id WHERE e.user_id=:u AND e.requires_approval=1 AND e.approved=0 ORDER BY e.created_at DESC",{"u":u["id"]})
    applied_ids={x["job_id"] for x in apps}
    counts={"total":len(apps),"queued":sum(x["status"] in ("queued","approved_to_continue") for x in apps),"interview":sum(x["status"] in ("interview","assessment") for x in apps),"offer":sum(x["status"]=="offer" for x in apps),"rejected":sum(x["status"]=="rejected" for x in apps)}
    role_options=[x.strip() for x in (p["roles"] or "").split(",") if x.strip()]
    return page(r,"dashboard.html",user=u,profile=p,jobs=jobs,apps=apps,approvals=approvals,applied_ids=applied_ids,counts=counts,role_options=role_options,settings=sql("SELECT * FROM user_settings WHERE user_id=:u",{"u":u["id"]},True))


@app.get("/dashboard",response_class=HTMLResponse)
def dashboard(r:Request):
    return home(r)

@app.get("/jobs",response_class=HTMLResponse)
def jobs_page(r:Request):
    u=need(r);p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    jobs=sql("SELECT j.*,COALESCE(m.ai_score,m.match_score,0) score,COALESCE(m.matched_skills,'') matched_skills,COALESCE(m.missing_skills,'') missing_skills,COALESCE(m.match_reasons,'') match_reasons FROM jobs j LEFT JOIN job_matches m ON m.job_id=j.id AND m.user_id=:u ORDER BY score DESC,j.id DESC LIMIT 100",{"u":u["id"]})
    apps=sql("SELECT job_id FROM applications WHERE user_id=:u",{"u":u["id"]})
    applied_ids={x["job_id"] for x in apps}
    role_options=[x.strip() for x in (p["roles"] or "").split(",") if x.strip()]
    settings=sql("SELECT * FROM user_settings WHERE user_id=:u",{"u":u["id"]},True)
    return page(r,"jobs.html",user=u,profile=p,jobs=jobs,applied_ids=applied_ids,role_options=role_options,settings=settings)

@app.get("/applications",response_class=HTMLResponse)
def applications_page(r:Request):
    u=need(r);p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    apps=sql("SELECT a.*,j.title,j.company,j.location,j.url FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.user_id=:u ORDER BY a.updated_at DESC LIMIT 100",{"u":u["id"]})
    approvals=sql("SELECT e.*,j.title,j.company FROM application_events e JOIN jobs j ON j.id=e.job_id WHERE e.user_id=:u AND e.requires_approval=1 AND e.approved=0 ORDER BY e.created_at DESC",{"u":u["id"]})
    counts={"total":len(apps),"queued":sum(x["status"] in ("queued","approved_to_continue") for x in apps),"interview":sum(x["status"] in ("interview","assessment") for x in apps),"offer":sum(x["status"]=="offer" for x in apps),"rejected":sum(x["status"]=="rejected" for x in apps)}
    settings=sql("SELECT * FROM user_settings WHERE user_id=:u",{"u":u["id"]},True)
    return page(r,"applications.html",user=u,profile=p,apps=apps,approvals=approvals,counts=counts,settings=settings)

@app.get("/profile",response_class=HTMLResponse)
def profile_page(r:Request):
    u=need(r);p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    settings=sql("SELECT * FROM user_settings WHERE user_id=:u",{"u":u["id"]},True)
    return page(r,"profile.html",user=u,profile=p,settings=settings)

@app.get("/settings",response_class=HTMLResponse)
def settings_page(r:Request):
    u=need(r);p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    settings=sql("SELECT * FROM user_settings WHERE user_id=:u",{"u":u["id"]},True)
    if not settings:
        run("INSERT INTO user_settings(user_id,theme) VALUES(:u,'light') ON CONFLICT(user_id) DO NOTHING",{"u":u["id"]})
        settings=sql("SELECT * FROM user_settings WHERE user_id=:u",{"u":u["id"]},True)
    return page(r,"settings.html",user=u,profile=p,settings=settings)

@app.post("/settings/account")
def settings_account(r:Request,email:str=Form(...),password:str=Form(""),name:str=Form(""),phone:str=Form("")):
    u=need(r);email=email.strip().lower()
    other=sql("SELECT id FROM users WHERE email=:e AND id<>:u",{"e":email,"u":u["id"]},True)
    if other: raise HTTPException(400,"That email is already in use.")
    run("UPDATE users SET email=:e WHERE id=:u",{"e":email,"u":u["id"]})
    run("UPDATE profiles SET name=:n,phone=:p WHERE user_id=:u",{"n":name,"p":phone,"u":u["id"]})
    if password:
        if len(password)<8: raise HTTPException(400,"Password must be at least 8 characters.")
        run("UPDATE users SET password_hash=:p WHERE id=:u",{"p":ph(password),"u":u["id"]})
    return RedirectResponse("/settings",303)

@app.post("/settings/theme")
def settings_theme(r:Request,theme:str=Form(...)):
    u=need(r);theme="dark" if theme=="dark" else "light"
    run("INSERT INTO user_settings(user_id,theme,updated_at) VALUES(:u,:t,CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET theme=:t,updated_at=CURRENT_TIMESTAMP",{"u":u["id"],"t":theme})
    return RedirectResponse("/settings",303)

@app.post("/profile")
def profile(r:Request,name:str=Form(""),phone:str=Form(""),roles:str=Form(""),locations:str=Form(""),skills:str=Form(""),min_score:int=Form(70),mode:str=Form("smart"),experience:str=Form(""),work_mode:str=Form("Any"),min_salary:str=Form(""),auto_apply_enabled:str=Form("")):
    u=need(r);run("""UPDATE profiles SET name=:n,phone=:p,roles=:r,locations=:l,skills=:s,min_score=:m,mode=:mo,experience=:e,work_mode=:w,min_salary=:sal,auto_apply_enabled=:a,daily_runs=4 WHERE user_id=:u""",{"n":name,"p":phone,"r":roles,"l":locations,"s":skills,"m":max(0,min(100,min_score)),"mo":mode,"e":experience,"w":work_mode,"sal":min_salary,"a":1 if auto_apply_enabled else 0,"u":u["id"]})
    return RedirectResponse("/profile",303)

@app.post("/resume")
def upload(r:Request,resume:UploadFile=File(...)):
    u=need(r);data=resume.file.read()
    if len(data)>8*1024*1024:raise HTTPException(400,"Resume must be 8 MB or smaller.")
    try:t=resume_text(data,resume.filename or "")
    except ValueError as e:raise HTTPException(400,str(e))
    name=re.sub(r"[^a-zA-Z0-9._-]","_",resume.filename or "resume")
    (UPLOADS/f"user_{u['id']}_{name}").write_bytes(data)
    run("UPDATE profiles SET resume_text=:t WHERE user_id=:u",{"t":t,"u":u["id"]});run("INSERT INTO resumes(user_id,filename,extracted_text) VALUES(:u,:n,:t)",{"u":u["id"],"n":name,"t":t})
    return RedirectResponse("/profile",303)

def live_jobs(p):
    """Fetch fresh jobs from public/optional APIs. Sources are normalized into our jobs schema."""
    roles=[x.strip() for x in (p.get("roles") or "").split(",") if x.strip()]
    skills=[x.strip() for x in (p.get("skills") or "").split(",") if x.strip()]
    locations=[x.strip() for x in (p.get("locations") or "").split(",") if x.strip()]
    queries=list(dict.fromkeys(roles+skills[:5]))[:8]
    rows=[]
    headers={"User-Agent":"JobPilot-AI/0.3 (+https://jobpilot-ai-0wsh.onrender.com)"}
    try:
        for page_no in range(1,3):
            data=httpx.get("https://www.arbeitnow.com/api/job-board-api",params={"page":page_no},headers=headers,timeout=12).json()
            for x in data.get("data",[]):
                rows.append({"source":"arbeitnow","external_id":str(x.get("slug") or x.get("id") or x.get("url")),
                    "title":x.get("title",""),"company":x.get("company_name",""),"location":x.get("location",""),
                    "url":x.get("url",""),"description":x.get("description",""),"posted_at":x.get("created_at") or x.get("created")})
    except Exception:
        pass
    try:
        for q in queries:
            data=httpx.get("https://remotive.com/api/remote-jobs",params={"search":q,"limit":50},headers=headers,timeout=12).json()
            for x in data.get("jobs",[]):
                rows.append({"source":"remotive","external_id":str(x.get("id")),
                    "title":x.get("title",""),"company":x.get("company_name",""),"location":x.get("candidate_required_location","Remote"),
                    "url":x.get("url",""),"description":x.get("description",""),"posted_at":x.get("publication_date")})
    except Exception:
        pass
    # Optional Adzuna gives strong India/local coverage when credentials are configured.
    app_id=os.getenv("ADZUNA_APP_ID"); app_key=os.getenv("ADZUNA_APP_KEY")
    if app_id and app_key:
        for q in queries[:5]:
            try:
                data=httpx.get(f"https://api.adzuna.com/v1/api/jobs/in/search/1",params={
                    "app_id":app_id,"app_key":app_key,"results_per_page":50,"what":q,
                    "content-type":"application/json","sort_by":"date"},headers=headers,timeout=12).json()
                for x in data.get("results",[]):
                    rows.append({"source":"adzuna","external_id":str(x.get("id")),
                        "title":x.get("title",""),"company":(x.get("company") or {}).get("display_name",""),
                        "location":(x.get("location") or {}).get("display_name","India"),
                        "url":x.get("redirect_url",""),"description":x.get("description",""),"posted_at":x.get("created")})
            except Exception:
                pass
    # De-duplicate and remove obviously stale/irrelevant records before ranking.
    seen=set(); out=[]
    preferred_locations=[x.lower() for x in locations if x.strip()]
    for j in rows:
        key=(j["source"],j["external_id"])
        if key in seen or not j["title"] or not j["url"]: continue
        seen.add(key)
        text_blob=(j["title"]+" "+j["description"]+" "+j["location"]).lower()
        if preferred_locations and not any(loc in text_blob for loc in preferred_locations) and "remote" not in text_blob and "worldwide" not in text_blob:
            continue
        j["source"]="live:"+j["source"]
        out.append(j)
    return out

def add_jobs(r,rows):
    u=need(r);p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    now=datetime.now(timezone.utc).isoformat()
    for j in rows:
        j={
            "source":j.get("source","manual"),
            "external_id":str(j.get("external_id") or j.get("url") or j.get("title")),
            "title":j.get("title","").strip(),
            "company":j.get("company","").strip(),
            "location":j.get("location","").strip(),
            "url":j.get("url","").strip(),
            "description":j.get("description","") or "",
            "posted_at":j.get("posted_at") or "",
        }
        if not j["title"] or not j["url"]: continue
        old=sql("SELECT id FROM jobs WHERE source=:s AND external_id=:e",{"s":j["source"],"e":j["external_id"]},True)
        if old:
            jid=old["id"]
            run("""UPDATE jobs SET title=:t,company=:c,location=:l,url=:url,description=:d,
                    posted_at=COALESCE(NULLIF(:posted_at,''),posted_at),last_seen_at=:seen WHERE id=:id""",
                {"t":j["title"],"c":j["company"],"l":j["location"],"url":j["url"],"d":j["description"],
                 "posted_at":j["posted_at"],"seen":now,"id":jid})
        else:
            run("""INSERT INTO jobs(source,external_id,title,company,location,url,description,posted_at,last_seen_at)
                   VALUES(:source,:external_id,:title,:company,:location,:url,:description,:posted_at,:seen)
                   ON CONFLICT(source,external_id) DO NOTHING""",
                {**j,"seen":now})
            row=sql("SELECT id FROM jobs WHERE source=:s AND external_id=:e",{"s":j["source"],"e":j["external_id"]},True)
            if not row: continue
            jid=row["id"]
        result=match_job(j,p)
        run("""INSERT INTO job_matches(user_id,job_id,match_score,ai_score,matched_skills,missing_skills,match_reasons)
               VALUES(:u,:j,:s,:s,:ms,:miss,:reasons)
               ON CONFLICT(user_id,job_id) DO UPDATE SET
                 match_score=excluded.match_score,ai_score=excluded.ai_score,
                 matched_skills=excluded.matched_skills,missing_skills=excluded.missing_skills,
                 match_reasons=excluded.match_reasons,matched_at=CURRENT_TIMESTAMP""",
            {"u":u["id"],"j":jid,"s":result["score"],
             "ms":json.dumps(result["matched_skills"]), "miss":json.dumps(result["missing_skills"]),
             "reasons":json.dumps(result["reasons"])})

@app.post("/jobs/import")
def imp(r:Request,payload:str=Form(...)):
    try:rows=json.loads(payload);assert isinstance(rows,list)
    except:raise HTTPException(400,"JSON must be an array")
    add_jobs(r,rows);return RedirectResponse("/",303)
@app.post("/jobs/refresh")
def refresh(r:Request,selected_role:str=Form("")):
    u=need(r)
    if selected_role:
        run("UPDATE profiles SET roles=:r WHERE user_id=:u",{"r":selected_role,"u":u["id"]})
    p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    rows=live_jobs(p)
    if not rows:
        raise HTTPException(503,"Live job sources are temporarily unavailable. Please try again.")
    add_jobs(r,rows)
    return RedirectResponse("/jobs",303)
@app.post("/applications/{jid}")
def application(r:Request,jid:int):
    u=need(r);j=sql("SELECT * FROM jobs WHERE id=:j",{"j":jid},True)
    if not j:raise HTTPException(404,"Job not found")
    p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    a=json.dumps({"why_this_role":f"I am interested in {j['title']} at {j['company']} because it matches my {p['skills']}.","summary":f"Candidate targeting {p['roles']}."})
    run("INSERT INTO applications(user_id,job_id,status,answers,notes,updated_at) VALUES(:u,:j,'queued',:a,:n,CURRENT_TIMESTAMP) ON CONFLICT(user_id,job_id) DO UPDATE SET status='queued',answers=:a,notes=:n,updated_at=CURRENT_TIMESTAMP",{"u":u["id"],"j":jid,"a":a,"n":"Queued for authorized automated submission."})
    event(u["id"],jid,"queued","application_queued","Application queued for automated submission.")
    return RedirectResponse("/applications",303)

@app.get("/api/job/{jid}/suggestions")
def suggestions(r:Request,jid:int):
    u=need(r);j=sql("SELECT * FROM jobs WHERE id=:j",{"j":jid},True);p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    if not j: raise HTTPException(404,"Job not found")
    desc=(j["description"] or "").lower();skills=[x.strip() for x in (p["skills"] or "").split(",") if x.strip()]
    missing=[s for s in skills if s.lower() not in desc];tips=[]
    if missing: tips.append("Consider highlighting: "+", ".join(missing[:5])+" if you have hands-on experience.")
    if p["experience"] and p["experience"].lower() not in desc: tips.append("Tailor your resume summary to explicitly state "+p["experience"]+".")
    if j["location"] and p["work_mode"]!="Any" and p["work_mode"].lower() not in j["location"].lower(): tips.append("Check work-mode compatibility before applying.")
    if not tips: tips.append("Your current profile is broadly aligned; tailor the top 2–3 resume bullets to this job description.")
    return {"job_id":jid,"suggestions":tips}

@app.post("/applications/{jid}/approval")
def approve_application(r:Request,jid:int):
    u=need(r)
    ev=sql("SELECT id FROM application_events WHERE user_id=:u AND job_id=:j AND requires_approval=1 AND approved=0 ORDER BY id DESC LIMIT 1",{"u":u["id"],"j":jid},True)
    if not ev:raise HTTPException(404,"No pending approval for this application.")
    run("UPDATE application_events SET approved=1,status='approved' WHERE id=:id",{"id":ev["id"]})
    run("UPDATE applications SET status='approved_to_continue',updated_at=CURRENT_TIMESTAMP WHERE user_id=:u AND job_id=:j",{"u":u["id"],"j":jid})
    event(u["id"],jid,"approved","user_approved","User approved continuation after a required checkpoint.")
    return RedirectResponse("/applications",303)
@app.post("/automation/run-now")
def run_now(r:Request,selected_role:str=Form("")):
    refresh(r,selected_role);return RedirectResponse("/jobs",303)
@app.get("/api/jobs")
def api(r:Request):
    u=need(r);return sql("SELECT j.*,COALESCE(m.match_score,0) score FROM jobs j LEFT JOIN job_matches m ON m.job_id=j.id AND m.user_id=:u ORDER BY score DESC",{"u":u["id"]})
@app.get("/health")
def health():
    try:sql("SELECT 1");return {"status":"ok","version":"0.3.0","database":"connected","multi_user":True,"max_users":5,"paid_ai_required":False,"scheduled_runs_per_day":4}
    except Exception as e:return {"status":"degraded","detail":str(e)}
