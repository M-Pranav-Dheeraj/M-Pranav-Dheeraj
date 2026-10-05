from pathlib import Path
import io, json, re, sqlite3
from fastapi import FastAPI, File, Form, Request, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader
from docx import Document

BASE = Path(__file__).resolve().parent
DB = BASE.parent / "jobpilot.db"
UPLOADS = BASE.parent / "uploads"
UPLOADS.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="JobPilot AI", version="0.1.0")

DEFAULT_PROFILE = {
    "name": "", "email": "", "phone": "",
    "roles": "Data Engineer, Data Analyst, AI/ML Engineer, Software Engineer, Python Developer",
    "locations": "India, Hyderabad, Bangalore, Pune, Chennai, Remote",
    "skills": "Python, SQL, Snowflake, AWS, Azure, Machine Learning, Pandas, NumPy, TensorFlow, Scikit-learn, Power BI, C++, C#/.NET",
    "min_score": 70, "mode": "smart"
}

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, email TEXT, phone TEXT, roles TEXT, locations TEXT, skills TEXT, min_score INTEGER, mode TEXT, resume_text TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, external_id TEXT, title TEXT, company TEXT, location TEXT, url TEXT, description TEXT, score REAL DEFAULT 0, status TEXT DEFAULT 'new', created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(source, external_id))")
    con.execute("CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER UNIQUE, status TEXT DEFAULT 'prepared', resume_version TEXT, answers TEXT, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(job_id) REFERENCES jobs(id))")
    if not con.execute("SELECT 1 FROM profile WHERE id=1").fetchone():
        con.execute("INSERT INTO profile(id,name,email,phone,roles,locations,skills,min_score,mode,resume_text) VALUES(1,?,?,?,?,?,?,?,?,?)",
                    [DEFAULT_PROFILE[k] for k in ("name","email","phone","roles","locations","skills","min_score","mode")] + [""])
        con.commit()
    return con

def tokens(text):
    return set(re.findall(r"[a-zA-Z0-9+#.]{2,}", (text or "").lower()))

def score_job(job, profile):
    desc = f"{job['title']} {job['company']} {job['location']} {job['description']}".lower()
    skills, roles, locs, d = tokens(profile["skills"]), tokens(profile["roles"]), tokens(profile["locations"]), tokens(desc)
    skill_hit = len(skills & d) / max(1, len(skills))
    role_hit = len(roles & d) / max(1, len(roles))
    loc_hit = 1 if any(x in desc for x in locs) or "remote" in desc else 0
    return round(min(100, 55*skill_hit + 30*role_hit + 15*loc_hit), 1)

def extract_resume(data, filename):
    if filename.lower().endswith(".pdf"):
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    if filename.lower().endswith(".docx"):
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    raise ValueError("Upload a PDF or DOCX resume")

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    con=db()
    profile=con.execute("SELECT * FROM profile WHERE id=1").fetchone()
    jobs=con.execute("SELECT * FROM jobs ORDER BY score DESC,id DESC LIMIT 100").fetchall()
    apps=con.execute("SELECT a.*,j.title,j.company FROM applications a JOIN jobs j ON j.id=a.job_id ORDER BY a.updated_at DESC LIMIT 20").fetchall()
    return templates.TemplateResponse(request=request,name="index.html",context={"profile":profile,"jobs":jobs,"apps":apps})

@app.post("/profile")
def save_profile(name:str=Form(""),email:str=Form(""),phone:str=Form(""),roles:str=Form(""),locations:str=Form(""),skills:str=Form(""),min_score:int=Form(70),mode:str=Form("smart")):
    con=db()
    con.execute("UPDATE profile SET name=?,email=?,phone=?,roles=?,locations=?,skills=?,min_score=?,mode=? WHERE id=1",[name,email,phone,roles,locations,skills,max(0,min(100,min_score)),mode])
    con.commit()
    return RedirectResponse("/",status_code=303)

@app.post("/resume")
def upload_resume(resume:UploadFile=File(...)):
    data=resume.file.read()
    try: text=extract_resume(data,resume.filename or "resume.pdf")
    except ValueError as e: raise HTTPException(400,str(e))
    (UPLOADS/re.sub(r"[^a-zA-Z0-9._-]","_",resume.filename or "resume")).write_bytes(data)
    con=db(); con.execute("UPDATE profile SET resume_text=? WHERE id=1",[text]); con.commit()
    return RedirectResponse("/",status_code=303)

@app.post("/jobs/import")
def import_jobs(payload:str=Form(...)):
    con=db(); p=con.execute("SELECT * FROM profile WHERE id=1").fetchone()
    try: rows=json.loads(payload)
    except Exception as e: raise HTTPException(400,f"Invalid JSON: {e}")
    if not isinstance(rows,list): raise HTTPException(400,"JSON must be an array of jobs")
    for j in rows:
        job={"source":j.get("source","manual"),"external_id":str(j.get("external_id") or j.get("url") or j.get("title")),"title":j.get("title",""),"company":j.get("company",""),"location":j.get("location",""),"url":j.get("url",""),"description":j.get("description","")}
        job["score"]=score_job(job,p)
        con.execute("INSERT INTO jobs(source,external_id,title,company,location,url,description,score) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET title=excluded.title,company=excluded.company,location=excluded.location,url=excluded.url,description=excluded.description,score=excluded.score",list(job.values()))
    con.commit()
    return RedirectResponse("/",status_code=303)

@app.post("/jobs/refresh")
def refresh_demo_jobs():
    sample=[
      {"source":"demo","external_id":"de-001","title":"Junior Data Engineer","company":"Example Data","location":"Hyderabad / Remote","url":"https://example.com/jobs/data-engineer","description":"Python SQL Snowflake ETL data pipelines AWS entry level analytics"},
      {"source":"demo","external_id":"de-002","title":"AI/ML Engineer - Fresher","company":"Example AI","location":"Bangalore","url":"https://example.com/jobs/ml","description":"Python machine learning TensorFlow scikit-learn pandas model development entry level"},
      {"source":"demo","external_id":"de-003","title":"Software Engineer","company":"Example Cloud","location":"Pune","url":"https://example.com/jobs/software","description":"C++ Python SQL REST API cloud software engineering graduate"}]
    con=db(); p=con.execute("SELECT * FROM profile WHERE id=1").fetchone()
    for j in sample:
        j["score"]=score_job(j,p)
        con.execute("INSERT OR REPLACE INTO jobs(source,external_id,title,company,location,url,description,score) VALUES(?,?,?,?,?,?,?,?)",list(j.values()))
    con.commit()
    return RedirectResponse("/",status_code=303)

@app.post("/applications/{job_id}")
def prepare_application(job_id:int):
    con=db(); job=con.execute("SELECT * FROM jobs WHERE id=?",[job_id]).fetchone()
    if not job: raise HTTPException(404,"Job not found")
    p=con.execute("SELECT * FROM profile WHERE id=1").fetchone()
    answers={"why_this_role":f"I am interested in the {job['title']} opportunity at {job['company']} because it aligns with my background in {p['skills']}.","summary":f"Entry-level candidate targeting {p['roles']} with hands-on project experience in Python, SQL and data/ML workflows."}
    con.execute("INSERT OR REPLACE INTO applications(job_id,status,resume_version,answers,notes) VALUES(?,?,?,?,?)",[job_id,"prepared","master",json.dumps(answers),"Ready for review"])
    con.execute("UPDATE jobs SET status='prepared' WHERE id=?",[job_id]); con.commit()
    return RedirectResponse("/",status_code=303)

@app.get("/api/jobs")
def api_jobs():
    return [dict(x) for x in db().execute("SELECT * FROM jobs ORDER BY score DESC")]

@app.get("/health")
def health(): return {"status":"ok","service":"jobpilot-ai","version":"0.1.0"}
