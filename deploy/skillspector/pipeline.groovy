pipeline {
    agent any

    options {
        timestamps()
        skipDefaultCheckout()
        quietPeriod(__JENKINS_QUIET_PERIOD__)
        buildDiscarder(logRotator(daysToKeepStr: '30', numToKeepStr: '10000'))
    }

    environment {
        PATH = "/opt/venv/bin:/usr/local/bin:${env.PATH}"
        REPORT_DIR = "${WORKSPACE}/reports"
        LOCAL_REPO_ROOT = "${env.WITTYHUB_REPOSITORY_ROOT ?: '/opt/wittyhub/skill-repositories'}"
    }

    parameters {
        string(name: 'GIT_URL', defaultValue: 'https://github.com/JunchengDwain/SkillSpector.git',
               description: 'Git Repository URL to clone')
        string(name: 'REF', defaultValue: 'main',
               description: 'Branch / Tag / Commit SHA')
        string(name: 'SKILL_PATH', defaultValue: '',
               description: 'Relative Skill Path (leave empty for repo root)')
        string(name: 'SCANNERS', defaultValue: 'skillspector',
               description: 'Comma separated scanners')
    }

    stages {
        stage("Checkout") {
            steps {
                script {
                    deleteDir()
                    withEnv([
                        "SCAN_GIT_URL=${params.GIT_URL?.trim()}",
                        "SCAN_GIT_REF=${params.REF?.trim() ?: 'main'}",
                        "SCAN_SKILL_PATH=${params.SKILL_PATH?.trim() ?: ''}",
                    ]) {
                        sh "wittyhub-skillspector-checkout"
                    }
                }
            }
        }

        stage("Detect Skill") {
            steps {
                script {
                    def skillDir
                    if (params.SKILL_PATH?.trim()) {
                        skillDir = "${env.WORKSPACE}/${params.SKILL_PATH.trim()}"
                    } else {
                        skillDir = env.WORKSPACE
                    }
                    env.SKILL_DIR = skillDir
                    sh '''
                        set -eu
                        test -d "$SKILL_DIR"
                        realpath "$SKILL_DIR"
                        ls -la "$SKILL_DIR"/
                    '''

                    def scannerList = params.SCANNERS.split(",").collect { it.trim() }.findAll { it }
                    env.SCANNER_LIST = scannerList.join(",")
                    echo "Scan dir  : ${env.SKILL_DIR}"
                    echo "Scanners  : ${scannerList}"
                }
            }
        }

        stage("Parallel Scan") {
            steps {
                script {
                    def jobs = [:]
                    def scannerList = env.SCANNER_LIST.split(",").findAll { it }
                    scannerList.each { scanner ->
                        jobs[scanner] = {
                            switch(scanner) {
                                case "skillspector":
                                    sh """
                                        set -ux

                                        mkdir -p "${REPORT_DIR}/${scanner}"

                                        # 大模型调用范围由容器环境变量决定（deploy/compose.yaml）：
                                        #   SKILLSPECTOR_LLM_NODES=meta_analyzer —— 只有 meta 复核节点调用
                                        #   大模型（剔除静态误报 + 富化），语义发现节点不调用；因此这里
                                        #   不能再加 --no-llm，否则会退化成纯静态扫描
                                        # skillspector 扫描到严重风险时即使已写出报告也会返回非零退出码，
                                        # 用 || true 忽略其退出码，改以 report.json 是否存在作为成功判据
                                        skillspector scan \\
                                            "${env.SKILL_DIR}" \\
                                            --format json -o "${REPORT_DIR}/${scanner}/report.json" \\
                                            --format markdown -o "${REPORT_DIR}/${scanner}/report.md" || true

                                        test -s "${REPORT_DIR}/${scanner}/report.json"
                                    """
                                    break
                                default:
                                    error("Unknown scanner: ${scanner}")
                            }
                        }
                    }
                    parallel jobs
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'reports/**/*', fingerprint: true, allowEmptyArchive: true
            deleteDir()
        }
    }
}
