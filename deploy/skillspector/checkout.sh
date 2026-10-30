#!/bin/sh

set -eu

# 部分平台（如未启用 LFS 的 AtomGit 项目）无法下载 Git LFS 大文件。
# 跳过 LFS smudge，让 git cat-file 导出 LFS 指针文件（文本）而非二进制内容，
# 避免 checkout 阶段因下载 LFS blob 失败导致整个扫描失败。
export GIT_LFS_SKIP_SMUDGE=1

git_url=${SCAN_GIT_URL:?SCAN_GIT_URL is required}
git_ref=${SCAN_GIT_REF:-main}
skill_path=${SCAN_SKILL_PATH:-}
repository_root=${LOCAL_REPO_ROOT:-/opt/wittyhub/skill-repositories}

case "$skill_path" in
    /*|../*|*/../*|*/..)
        echo "Invalid skill path: $skill_path" >&2
        exit 2
        ;;
esac

repository_key=$(
    printf '%s' "$git_url" |
        sed -e 's#^https\?://##' -e 's#/$##' -e 's#\.git$##' -e 's#/#_#g'
)

case "$repository_key" in
    ''|*[!A-Za-z0-9._-]*)
        echo "Local repository lookup skipped: unsupported URL format: $git_url"
        repository_key=''
        ;;
esac

local_repository=''
if [ -n "$repository_key" ]; then
    local_repository="${repository_root}/${repository_key}"
fi

# 确定 source_repo 和 resolved_commit
# 优先使用本地缓存仓库（只读），否则从远程 fetch 到工作空间仓库。
source_repo=''
resolved_commit=''

if [ -n "$local_repository" ] && [ -d "${local_repository}/.git" ] && \
    git -c safe.directory="$local_repository" -C "$local_repository" \
        cat-file -e "${git_ref}^{commit}" 2>/dev/null; then
    echo "Using local repository cache: $local_repository"
    source_repo="$local_repository"
    resolved_commit=$(git -c safe.directory="$local_repository" \
        -C "$local_repository" rev-parse "${git_ref}^{commit}")
else
    if [ -n "$local_repository" ] && [ -d "${local_repository}/.git" ]; then
        echo "Reference is not available locally; fetching from origin: $git_ref"
    else
        echo "Local repository cache not found; using remote repository: $git_url"
    fi
    git init -q
    git config filter.lfs.smudge cat
    git config filter.lfs.process ''
    git config filter.lfs.required false
    git remote add origin "$git_url"
    git fetch --depth 1 origin "$git_ref"
    source_repo=.
    resolved_commit=$(git rev-parse "FETCH_HEAD^{commit}")
fi

echo "Resolved commit: $resolved_commit"

# 不能使用 git archive：它会遵循 .gitattributes 中的 export-ignore 规则
# （例如 astronomer/airflow 对 .agents 设置了 export-ignore），导致导出内容为空。
# 也不能使用 git checkout：partial clone 环境下它会尝试从 promisor remote
# 拉取 blob，在无网络容器中会超时失败（即使 blob 通过 alternates 可用）。
# 改为直接用 cat-file 从 source_repo 逐个提取文件，完全绕过这两个问题。
extract_tree() {
    _ref="$1"
    _path="$2"
    _src="$3"
    git -c safe.directory="$_src" -C "$_src" ls-tree -r \
        --format='%(objectmode) %(objecttype) %(objectname) %(path)' \
        "$_ref" -- "$_path" |
    while IFS=' ' read -r mode type sha path; do
        case "$type" in
            blob)
                dir=$(dirname "$path")
                [ -d "$dir" ] || mkdir -p "$dir"
                git -c safe.directory="$_src" -C "$_src" cat-file -p "$sha" > "$path"
                case "$mode" in
                    100*) chmod "${mode#100}" "$path" 2>/dev/null || true ;;
                esac
                ;;
            commit)
                : # submodule — 跳过
                ;;
        esac
    done
}

if [ -n "$skill_path" ]; then
    echo "Exporting skill path: $skill_path"
    if ! git -c safe.directory="$source_repo" -C "$source_repo" \
        cat-file -e "${resolved_commit}:${skill_path}" 2>/dev/null; then
        echo "Skill path does not exist at commit ${resolved_commit}: ${skill_path}" >&2
        exit 3
    fi
    extract_tree "$resolved_commit" "$skill_path" "$source_repo"
else
    echo "Exporting repository root"
    extract_tree "$resolved_commit" "." "$source_repo"
fi

echo "Prepared scan content from commit: $resolved_commit"
