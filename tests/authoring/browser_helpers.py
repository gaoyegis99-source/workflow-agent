from playwright.sync_api import expect


def connect_workspace(page, origin, token, project_id, section):
    page.goto(origin + "/")
    page.get_by_label("临时 Bearer Token").fill(token)
    page.get_by_role("button", name="连接并加载项目").click()
    expect(page.locator(".project-picker select")).to_be_visible(timeout=20_000)
    page.locator(".project-picker select").select_option(project_id)
    page.get_by_role("button", name="进入工作区").click()
    if section != "overview":
        names = {
            "assets": "数据资产",
            "tasks": "任务",
            "runs": "运行",
            "workflows": "工作流",
            "releases": "发布版本",
            "operators": "算子目录",
        }
        page.get_by_role("link", name=names[section], exact=True).click()


def create_workspace(page, origin, token, name, section="overview"):
    page.goto(origin + "/")
    page.get_by_label("临时 Bearer Token").fill(token)
    page.get_by_role("button", name="连接并加载项目").click()
    expect(page.locator(".project-picker")).to_be_visible(timeout=20_000)
    page.get_by_label("项目名称").fill(name)
    page.get_by_role("button", name="创建项目").click()
    project_id = page.locator(".project-picker select").input_value()
    page.get_by_role("button", name="进入工作区").click()
    if section != "overview":
        names = {
            "assets": "数据资产",
            "tasks": "任务",
            "runs": "运行",
            "workflows": "工作流",
            "releases": "发布版本",
            "operators": "算子目录",
        }
        page.get_by_role("link", name=names[section], exact=True).click()
    return project_id


def upload_asset(page, path, columns=None):
    page.get_by_role("button", name="上传数据").click()
    page.locator('input[type="file"]').set_input_files(path)
    page.get_by_role("button", name="上传并继续").click()
    expect(page.get_by_text("已上传，摘要与大小已经确认", exact=False)).to_be_visible(
        timeout=20_000
    )
    if columns is not None:
        page.get_by_label("列定义（JSON）").fill(columns)
    page.get_by_role("button", name="检查并导入").click()
    expect(page.get_by_text("导入请求已确认")).to_be_visible(timeout=20_000)
    page.get_by_role("button", name="完成").click()
    expect(page.locator(".ui-table tbody tr.clickable").first).to_be_visible(timeout=40_000)


def connect_task(page, origin, token, project_id, task_id):
    page.goto(f"{origin}/projects/{project_id}/tasks/{task_id}")
    page.get_by_label("临时 Bearer Token").fill(token)
    page.get_by_role("button", name="连接并加载项目").click()
    expect(page.get_by_role("tab", name="范围")).to_be_visible(timeout=20_000)
