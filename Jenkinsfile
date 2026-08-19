// SixRabbits Stock 发布流水线（master → 生产 47.99.214.159）
//
// Jenkins 与生产同机：构建在本机完成，部署通过 loopback SSH 复用 scripts/deploy.py
// （release+软链、复制 .env、pip 依赖、沙箱镜像、滚动重启、健康验证、失败自动回滚）。
pipeline {
    agent any

    options {
        disableConcurrentBuilds()                    // 发布任务串行，避免并发切换软链
        timestamps()
        buildDiscarder(logRotator(numToKeepStr: '30'))
        timeout(time: 40, unit: 'MINUTES')
    }

    triggers {
        pollSCM('H/5 * * * *')                       // 每 5 分钟轮询 master（Gitee 未配 webhook）
    }

    environment {
        PATH = "/opt/node/bin:${env.PATH}"           // node v24 仅 CI 用，不进全局环境
        npm_config_registry = 'https://registry.npmmirror.com'
    }

    stages {
        stage('构建前端') {
            steps {
                dir('frontend') {
                    sh 'npm ci --no-audit --no-fund'
                    sh 'npm run build'               // vue-tsc 类型检查 + vite 打包，失败即终止
                }
            }
        }

        stage('发布到生产') {
            steps {
                // deploy.py 经 loopback SSH 以 root 执行：
                // 新 release → 复制上一版 .env（不覆盖）→ 装依赖 → 切软链 → 滚动重启 → 验证，失败自动回滚
                sh 'python3 scripts/deploy.py --host 127.0.0.1 --user root --key /opt/jenkins/.ssh/id_ed25519'
            }
        }
    }

    post {
        success {
            echo '[OK] 发布成功，current 已指向新 release'
        }
        failure {
            echo '发布失败：deploy.py 已自动回滚到上一版，详见上方“发布到生产”阶段日志'
        }
    }
}
