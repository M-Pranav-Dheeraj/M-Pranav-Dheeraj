import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from app.main import score_job

def test_relevant_job_scores_higher():
    profile={"skills":"Python, SQL, Snowflake, AWS, Machine Learning", "roles":"Data Engineer, AI/ML Engineer", "locations":"Hyderabad, Bangalore, Remote"}
    good={"title":"Junior Data Engineer","company":"X","location":"Hyderabad","description":"Python SQL Snowflake AWS ETL data engineering"}
    bad={"title":"Graphic Designer","company":"Y","location":"Delhi","description":"Photoshop Illustrator branding visual design"}
    assert score_job(good,profile)>score_job(bad,profile)
