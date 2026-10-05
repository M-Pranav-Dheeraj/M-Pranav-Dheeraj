import io,json,os,re,hashlib,hmac,secrets
from pathlib import Path
from fastapi import FastAPI,File,Form,Request,UploadFile,HTTPException
from fastapi.responses import HTMLResponse,RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pypdf import PdfReader
from docx import Document
from sqlalchemy import create_engine,text

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
            c.execute(text("CREATE TABLE IF NOT EXISTS resumes(id SERIAL PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,filename TEXT,extracted_text TEXT,uploaded_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)"))
            c.execute(text("CREATE TABLE IF NOT EXISTS jobs(id SERIAL PRIMARY KEY,source TEXT NOT NULL,external_id TEXT NOT NULL,title TEXT,company TEXT,location TEXT,url TEXT,description TEXT,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,UNIQUE(source,external_id))"))
            c.execute(text("CREATE TABLE IF NOT EXISTS job_matches(user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,match_score REAL,matched_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,job_id))"))
            c.execute(text("CREATE TABLE IF NOT EXISTS applications(id SERIAL PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,status TEXT DEFAULT 'prepared',answers TEXT,notes TEXT,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,UNIQUE(user_id,job_id))"))
        else:
            c.execute(text("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP)"))
            c.execute(text("CREATE TABLE IF NOT EXISTS profiles(user_id INTEGER PRIMARY KEY,name TEXT DEFAULT '',phone TEXT DEFAULT '',roles TEXT DEFAULT '',locations TEXT DEFAULT '',skills TEXT DEFAULT '',min_score INTEGER DEFAULT 70,mode TEXT DEFAULT 'smart',experience TEXT DEFAULT '',work_mode TEXT DEFAULT 'Any',min_salary TEXT DEFAULT '',auto_apply_enabled INTEGER DEFAULT 0,daily_runs INTEGER DEFAULT 4,resume_text TEXT DEFAULT '')"))
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
    d=toks(" ".join(str(j.get(k,"")) for k in ("title","company","location","description")))
    sk,ro,lo=toks(p["skills"]),toks(p["roles"]),toks(p["locations"])
    return round(min(100,55*len(sk&d)/max(1,len(sk))+30*len(ro&d)/max(1,len(ro))+(15 if any(x in " ".join(d) for x in lo) or "remote" in d else 0)),1)
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
    r.session["uid"]=uid;return RedirectResponse("/",303)
@app.post("/logout")
def logout(r:Request):r.session.clear();return RedirectResponse("/login",303)

@app.get("/",response_class=HTMLResponse)
def home(r:Request):
    u=user(r)
    if not u:return RedirectResponse("/login",303)
    p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    jobs=sql("SELECT j.*,COALESCE(m.match_score,0) score FROM jobs j LEFT JOIN job_matches m ON m.job_id=j.id AND m.user_id=:u ORDER BY score DESC,j.id DESC LIMIT 100",{"u":u["id"]})
    apps=sql("SELECT a.*,j.title,j.company FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.user_id=:u ORDER BY a.updated_at DESC LIMIT 100",{"u":u["id"]})
    approvals=sql("SELECT e.*,j.title,j.company FROM application_events e JOIN jobs j ON j.id=e.job_id WHERE e.user_id=:u AND e.requires_approval=1 AND e.approved=0 ORDER BY e.created_at DESC",{"u":u["id"]})
    applied_ids={x["job_id"] for x in apps}
    counts={"total":len(apps),"queued":sum(x["status"] in ("queued","approved_to_continue") for x in apps),"interview":sum(x["status"] in ("interview","assessment") for x in apps),"offer":sum(x["status"]=="offer" for x in apps),"rejected":sum(x["status"]=="rejected" for x in apps)}
    role_options=[x.strip() for x in (p["roles"] or "").split(",") if x.strip()]
    return page(r,"index.html",user=u,profile=p,jobs=jobs,apps=apps,approvals=approvals,applied_ids=applied_ids,counts=counts,role_options=role_options)

@app.post("/profile")
def profile(r:Request,name:str=Form(""),phone:str=Form(""),roles:str=Form(""),locations:str=Form(""),skills:str=Form(""),min_score:int=Form(70),mode:str=Form("smart"),experience:str=Form(""),work_mode:str=Form("Any"),min_salary:str=Form(""),auto_apply_enabled:str=Form("")):
    u=need(r);run("""UPDATE profiles SET name=:n,phone=:p,roles=:r,locations=:l,skills=:s,min_score=:m,mode=:mo,experience=:e,work_mode=:w,min_salary=:sal,auto_apply_enabled=:a,daily_runs=4 WHERE user_id=:u""",{"n":name,"p":phone,"r":roles,"l":locations,"s":skills,"m":max(0,min(100,min_score)),"mo":mode,"e":experience,"w":work_mode,"sal":min_salary,"a":1 if auto_apply_enabled else 0,"u":u["id"]})
    return RedirectResponse("/",303)

@app.post("/resume")
def upload(r:Request,resume:UploadFile=File(...)):
    u=need(r);data=resume.file.read()
    if len(data)>8*1024*1024:raise HTTPException(400,"Resume must be 8 MB or smaller.")
    try:t=resume_text(data,resume.filename or "")
    except ValueError as e:raise HTTPException(400,str(e))
    name=re.sub(r"[^a-zA-Z0-9._-]","_",resume.filename or "resume")
    (UPLOADS/f"user_{u['id']}_{name}").write_bytes(data)
    run("UPDATE profiles SET resume_text=:t WHERE user_id=:u",{"t":t,"u":u["id"]});run("INSERT INTO resumes(user_id,filename,extracted_text) VALUES(:u,:n,:t)",{"u":u["id"],"n":name,"t":t})
    return RedirectResponse("/",303)

def add_jobs(r,rows):
    u=need(r);p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    for j in rows:
        j={"source":j.get("source","manual"),"external_id":str(j.get("external_id") or j.get("url") or j.get("title")),"title":j.get("title",""),"company":j.get("company",""),"location":j.get("location",""),"url":j.get("url",""),"description":j.get("description","")}
        old=sql("SELECT id FROM jobs WHERE source=:s AND external_id=:e",{"s":j["source"],"e":j["external_id"]},True)
        if old:jid=old["id"];run("UPDATE jobs SET title=:t,company=:c,location=:l,url=:url,description=:d WHERE id=:id",{"t":j["title"],"c":j["company"],"l":j["location"],"url":j["url"],"d":j["description"],"id":jid})
        else:
            run("INSERT INTO jobs(source,external_id,title,company,location,url,description) VALUES(:source,:external_id,:title,:company,:location,:url,:description) ON CONFLICT(source,external_id) DO NOTHING",j)
            jid=sql("SELECT id FROM jobs WHERE source=:s AND external_id=:e",{"s":j["source"],"e":j["external_id"]},True)["id"]
        z=score(j,p)
        run("INSERT INTO job_matches(user_id,job_id,match_score) VALUES(:u,:j,:s) ON CONFLICT(user_id,job_id) DO UPDATE SET match_score=excluded.match_score,matched_at=CURRENT_TIMESTAMP",{"u":u["id"],"j":jid,"s":z})
@app.post("/jobs/import")
def imp(r:Request,payload:str=Form(...)):
    try:rows=json.loads(payload);assert isinstance(rows,list)
    except:raise HTTPException(400,"JSON must be an array")
    add_jobs(r,rows);return RedirectResponse("/",303)
@app.post("/jobs/refresh")
def refresh(r:Request,selected_role:str=Form("")):
    if selected_role:
        u=need(r);run("UPDATE profiles SET roles=:r WHERE user_id=:u",{"r":selected_role,"u":u["id"]})
    add_jobs(r,[{"source":"demo","external_id":"de-001","title":"Junior Data Engineer","company":"Example Data","location":"Hyderabad / Remote","url":"https://example.com/jobs/data-engineer","description":"Python SQL Snowflake ETL data pipelines AWS entry level analytics"},{"source":"demo","external_id":"de-002","title":"AI/ML Engineer - Fresher","company":"Example AI","location":"Bangalore","url":"https://example.com/jobs/ml","description":"Python machine learning TensorFlow scikit-learn pandas model development entry level"},{"source":"demo","external_id":"de-003","title":"Software Engineer","company":"Example Cloud","location":"Pune","url":"https://example.com/jobs/software","description":"C++ Python SQL REST API cloud software engineering graduate"}]);return RedirectResponse("/",303)
@app.post("/applications/{jid}")
def application(r:Request,jid:int):
    u=need(r);j=sql("SELECT * FROM jobs WHERE id=:j",{"j":jid},True)
    if not j:raise HTTPException(404,"Job not found")
    p=sql("SELECT * FROM profiles WHERE user_id=:u",{"u":u["id"]},True)
    a=json.dumps({"why_this_role":f"I am interested in {j['title']} at {j['company']} because it matches my {p['skills']}.","summary":f"Candidate targeting {p['roles']}."})
    run("INSERT INTO applications(user_id,job_id,status,answers,notes,updated_at) VALUES(:u,:j,'queued',:a,:n,CURRENT_TIMESTAMP) ON CONFLICT(user_id,job_id) DO UPDATE SET status='queued',answers=:a,notes=:n,updated_at=CURRENT_TIMESTAMP",{"u":u["id"],"j":jid,"a":a,"n":"Queued for authorized automated submission."})
    event(u["id"],jid,"queued","application_queued","Application queued for automated submission.")
    return RedirectResponse("/",303)

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
    return RedirectResponse("/",303)
@app.post("/automation/run-now")
def run_now(r:Request,selected_role:str=Form("")):
    refresh(r,selected_role);return RedirectResponse("/",303)
@app.get("/api/jobs")
def api(r:Request):
    u=need(r);return sql("SELECT j.*,COALESCE(m.match_score,0) score FROM jobs j LEFT JOIN job_matches m ON m.job_id=j.id AND m.user_id=:u ORDER BY score DESC",{"u":u["id"]})
@app.get("/health")
def health():
    try:sql("SELECT 1");return {"status":"ok","version":"0.2.0","database":"connected","multi_user":True,"scheduled_runs_per_day":4}
    except Exception as e:return {"status":"degraded","detail":str(e)}
