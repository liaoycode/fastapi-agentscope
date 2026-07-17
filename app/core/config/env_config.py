# app/config.py
from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()

class Settings(BaseSettings):
    # app
    app_name: str
    app_host: str
    app_port: int

    # nacos
    nacos_enable: bool
    nacos_server: str
    nacos_namespace: str
    nacos_username: str
    nacos_password: str
    nacos_group: str
    nacos_server_name: str

    # database
    db_enable: bool
    database_url: str
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    db_detail_debug: bool = False

    # redis
    redis_enable: bool
    redis_url: str
    redis_max_connections: int

    class Config:
        env_file = ".env"

settings = Settings()
