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

    # llm
    llm_base_url:str
    llm_api_key:str
    llm_model_name:str
    # 模型上下文窗口大小(模型固有属性,AgentScope 用它算 trigger_ratio 阈值)
    llm_context_size: int = 128000

    # agentscope
    agent_enable: bool
    agent_system_prompt: str
    agent_workspace_image: str
    agent_workspace_mem_limit: str
    agent_skill_public_dir: str
    # 沙箱容器内的工作根目录。agentscope 的 DockerWorkspace 默认 /sandbox,
    # 我们通过 HardenedDockerWorkspace.__init__ 把它做成可配置 —— 既影响容器
    # WorkingDir / 命名卷挂载点,也是 PersonalSkillLoader 拼 Skill.dir 的依据。
    agent_workspace_container_dir: str = "/sandbox"
    # personal skill 在容器内的子目录名(拼到 container_dir 下);public skill
    # 固定用 "skills",personal 走这个名字以避免冲突。
    agent_skill_personal_subdir: str = "personal_skills"
    agent_skill_public_subdir: str = "skills"
    # 上下文压缩的 触发比例
    agent_context_trigger_ratio: float = 0.6
    # 压缩后保留多少最近的信息比例
    agent_context_reserve_ratio: float = 0.2

    # per-user sandbox & hardening
    agent_workspace_pids_limit: int = 512
    agent_workspace_cpu_shares: int = 512
    # 闲置多久回收 per-user 容器(秒)
    agent_workspace_idle_ttl_seconds: int = 600
    # 后台 reaper 巡检间隔(秒)
    agent_workspace_reaper_interval_seconds: int = 60

    # 远程 docker daemon 配置。
    # 默认走 unix socket(本机);远程沙箱主机时改成
    # "tcp://sandbox-host:2375" 之类。aiodocker 也读 DOCKER_HOST 环境变量,
    # 这里显式化是为了 .env 集中管理、不依赖运行环境。
    agent_docker_host: str = "unix:///var/run/docker.sock"
    agent_docker_tls_verify: bool = False

    # 命名卷前缀。Docker 命名卷由 daemon 持有,不依赖 host 路径,
    # docker-in-docker / 远端沙箱主机部署都兼容。
    agent_user_volume_prefix: str = "agent-sandbox"

    # workspace 快照(save→PG / restore→容器)。
    # 关闭容器前自动 save 一份到 PG,下次起 sandbox 时按需 restore。
    agent_snapshot_enabled: bool = True
    # auto-pre-close 滚动保留份数;manual 标签不滚动。
    agent_snapshot_max_auto_per_user: int = 5


    class Config:
        env_file = ".env"

settings = Settings()
