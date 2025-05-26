# routers/user.py
# 사용자 관련 API: 스타일 추천, 미용실 추천, 얼굴 분석 요청

from fastapi import APIRouter, Depends, Form, File, UploadFile, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel
from typing import List, Optional
from core.security import get_current_user  # 공통 인증 모듈 사용
import os
import boto3
from botocore.exceptions import NoCredentialsError
import uuid
from sqlalchemy.orm import Session
from models.request import Request
from core.database import get_db
from datetime import datetime
import requests
from models.result import Result
from sqlalchemy import desc
from models.hair_recommendation import HairRecommendation
from models.hairshop_recommendation import HairshopRecommendation

router = APIRouter()

# 응답 모델 정의 (ERD 기준 필드명 반영)
class Style(BaseModel):
    hair_id: int
    hairstyle_name: str
    hairstyle_image_url: str

class Hairshop(BaseModel):
    hairshop_id: int
    hairshop_name: str
    address: str
    link: str

class RecommendedStyle(BaseModel):
    hair_id: int
    hairstyle_name: str
    hairstyle_image_url: str
    description: str

class UserResultResponse(BaseModel):
    user_image_url: str
    sex: str
    face_type: str
    skin_tone: str
    rec_color: str
    summary: str

# face_extract 호출 함수 정의
def trigger_face_extract(user_id, request_id):
    try:
        # [개발용] Docker 내부 통신 주소 사용
        res = requests.post(
            "http://extract_face:8001/run-extract/",
            json={"user_id": user_id, "request_id": request_id},
            timeout=5
        )
        # [운영용] EC2 고정 IP 사용 시 아래로 교체
        # res = requests.post(
        #     "http://13.124.74.93:8001/run-extract/",
        #     json={"user_id": user_id, "request_id": request_id},
        #     timeout=5
        # )
        print(f"[INFO] face_extract 응답: {res.status_code}, {res.text}")
    except Exception as e:
        print(f"[ERROR] face_extract 호출 실패: {e}")

# 사용자별 추천 스타일 조회
@router.get("/user/hairstyles", response_model=List[Style])
def get_user_styles(current_user: dict = Depends(get_current_user)):
    return [
        {
            "hair_id": 1,
            "hairstyle_name": "가일컷",
            "hairstyle_image_url": "https://example.com/style1.jpg"
        },
        {
            "hair_id": 2,
            "hairstyle_name": "댄디컷",
            "hairstyle_image_url": "https://example.com/style2.jpg"
        }
    ]

# 사용자별 추천 미용실 조회
@router.get("/user/hairshops", response_model=List[Hairshop])
def get_user_hairshops(current_user: dict = Depends(get_current_user)):
    return [
        {
            "hairshop_id": 1,
            "hairshop_name": "살롱드헤어 신촌점",
            "address": "서울특별시 마포구 신촌로 45",
            "link": "https://hairshop.example.com/shop1"
        },
        {
            "hairshop_id": 2,
            "hairshop_name": "이철헤어커커 강남점",
            "address": "서울특별시 강남구 테헤란로 101",
            "link": "https://hairshop.example.com/shop2"
        }
    ]

# S3 업로드 함수
def upload_image_to_s3(file, bucket, region, access_key, secret_key, filename=None):
    s3 = boto3.client(
        's3',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region
    )
    if filename is None:
        filename = f"user_images/{uuid.uuid4()}_{file.filename}"
    if "YOUR_ACCESS_KEY" in access_key:
        print("[WARN] S3 접근 키가 .env에 지정되지 않았습니다.")    

    try:
        s3.upload_fileobj(
            file.file,
            bucket,
            filename,
            ExtraArgs={"ContentType": file.content_type}
        )
        url = f"https://{bucket}.s3.{region}.amazonaws.com/{filename}"
        return url
    except NoCredentialsError:
        raise Exception("AWS credentials not available")

# 얼굴 분석 요청 (설문 + 이미지)
@router.post("/analyze-face")
async def analyze_face(
    background_tasks: BackgroundTasks,
    hair_length: str = Form(...),
    hair_type: str = Form(...),
    sex: str = Form(...),
    location: str = Form(...),
    cheekbone: str = Form(...),
    mood: str = Form(...),
    dyed: str = Form(...),
    forehead_shape: str = Form(...),
    difficulty: str = Form(...),
    has_bangs: str = Form(...),
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        # 1. request_table에 임시 저장 (user_image_url은 빈 값)
        req = Request(
            user_id=current_user["user_id"],
            hair_length=hair_length,
            hair_type=hair_type,
            sex=sex,
            location=location,
            user_image_url="",  # 임시
            created_at=datetime.utcnow(),
            cheekbone=cheekbone,
            mood=mood,
            dyed=dyed,
            forehead_shape=forehead_shape,
            difficulty=difficulty,
            has_bangs=has_bangs
        )
        db.add(req)
        db.commit()
        db.refresh(req)

        # 2. S3에 업로드 (user_image_dic/{user_id}_{request_id}.png)
        filename = f"user_image_dic/{current_user['user_id']}_{req.request_id}.png"
        s3_url = upload_image_to_s3(
            image,
            bucket=os.getenv("AWS_S3_BUCKET", "YOUR_BUCKET_NAME"),
            region=os.getenv("AWS_S3_REGION", "YOUR_REGION"),
            access_key=os.getenv("AWS_ACCESS_KEY_ID", "YOUR_ACCESS_KEY"),
            secret_key=os.getenv("AWS_SECRET_ACCESS_KEY", "YOUR_SECRET_KEY"),
            filename=filename
        )

        # 3. user_image_url 업데이트
        req.user_image_url = s3_url
        db.commit()
        db.refresh(req)

        # 백그라운드 작업 등록
        background_tasks.add_task(trigger_face_extract, current_user["user_id"], req.request_id)
        
        return {
            "success": True,
            "message": "요청이 성공적으로 저장되었고 분석이 시작되었습니다.",
            "data": {
                "user_id": current_user["user_id"],
                "request_id": req.request_id,
                "image_url": s3_url
            }
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"요청 처리 중 오류가 발생했습니다: {str(e)}"
        )

# 추천 스타일 리스트 조회 (mock)
@router.get("/recommend/styles", response_model=List[RecommendedStyle])
def recommend_styles(current_user: dict = Depends(get_current_user)):
    return [
        {
            "hair_id": 1,
            "hairstyle_name": "리프컷",
            "hairstyle_image_url": "https://example.com/styles/leafcut.jpg",
            "description": "부드럽고 자연스러운 느낌을 주는 스타일입니다."
        },
        {
            "hair_id": 2,
            "hairstyle_name": "댄디컷",
            "hairstyle_image_url": "https://example.com/styles/dandycut.jpg",
            "description": "깔끔하고 단정한 인상을 주는 베스트셀러 스타일입니다."
        },
        {
            "hair_id": 3,
            "hairstyle_name": "쉐도우펌",
            "hairstyle_image_url": "https://example.com/styles/shadowperm.jpg",
            "description": "볼륨감 있게 연출되어 얼굴형 보완에 효과적입니다."
        }
    ]

@router.get("/user/result/{request_id}", response_model=UserResultResponse)
def get_user_result(request_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    # 1. request_table에서 이미지, 성별
    req = db.query(Request).filter(Request.request_id == request_id, Request.user_id == current_user["user_id"]).first()
    if not req:
        raise HTTPException(status_code=404, detail="해당 요청을 찾을 수 없습니다.")
    # 2. result_table에서 분석 결과
    result = db.query(Result).filter(Result.request_id == request_id).first()
    if not result:
        raise HTTPException(status_code=404, detail="아직 분석 결과가 저장되지 않았습니다.")
    return UserResultResponse(
        user_image_url=req.user_image_url,
        sex=req.sex,
        face_type=result.face_type,
        skin_tone=result.skin_tone,
        rec_color=result.rec_color,
        summary=result.summary
    )

@router.get("/user/latest-request-id")
def get_latest_request_id(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    req = db.query(Request).filter(Request.user_id == current_user["user_id"]).order_by(desc(Request.created_at)).first()
    if not req:
        return {"request_id": None}
    return {"request_id": req.request_id}

# (1) request_id로 추천 헤어 리스트 반환
class HairRecommendationResponse(BaseModel):
    hair_rec_id: int
    hair_name: str
    simulation_image_url: str
    description: str

@router.get("/user/hair-recommendations/{request_id}", response_model=List[HairRecommendationResponse])
def get_hair_recommendations(request_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    hairs = db.query(HairRecommendation).filter(HairRecommendation.request_id == request_id).all()
    return [
        {
            "hair_rec_id": h.hair_rec_id,
            "hair_name": h.hair_name,
            "simulation_image_url": h.simulation_image_url,
            "description": h.description
        }
        for h in hairs
    ]

# (2) hair_rec_id로 추천 미용실 리스트 반환 (리뷰수 내림차순 정렬, 스크롤/페이지네이션 지원)
class HairshopRecommendationResponse(BaseModel):
    hairshop: str
    review_count: int
    mean_score: float

@router.get("/user/hairshop-recommendations/{hair_rec_id}", response_model=List[HairshopRecommendationResponse])
def get_hairshop_recommendations(
    hair_rec_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1),
    db: Session = Depends(get_db)
):
    shops = db.query(HairshopRecommendation) \
        .filter(HairshopRecommendation.hair_rec_id == hair_rec_id) \
        .order_by(HairshopRecommendation.review_count.desc()) \
        .offset(skip).limit(limit).all()
    return [
        {
            "hairshop": s.hairshop,
            "review_count": s.review_count,
            "mean_score": s.mean_score
        }
        for s in shops
    ]
