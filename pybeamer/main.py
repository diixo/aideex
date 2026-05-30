
import os

from dotenv import load_dotenv

from pybeamer import Codebeamer


load_dotenv()

codebeamer_url = os.getenv('CODEBEAMER_URL')
codebeamer_username = os.getenv('CODEBEAMER_USERNAME')
codebeamer_password = os.getenv('CODEBEAMER_PASSWORD')
codebeamer_api_root = os.getenv('CODEBEAMER_API_ROOT', 'api/v3')


if not all([codebeamer_url, codebeamer_username, codebeamer_password]):
    raise ValueError(
        'Missing required environment variables: '
        'CODEBEAMER_URL, CODEBEAMER_USERNAME, CODEBEAMER_PASSWORD'
    )

codebeamer = Codebeamer(
    url=codebeamer_url,
    username=codebeamer_username,
    password=codebeamer_password,
    api_root=codebeamer_api_root
)

def get_current_time() -> str:
    from datetime import datetime
    result = datetime.now().isoformat()
    return result.replace('T', '_')


def tracker_type_id_by_ui_name(ui_name: str) -> int:
    type_map = codebeamer.get_tracker_types()
    names_ui2api = {
        "Area": "Area",
        "Bug": "Bug",
        "Change Request": "Change Request",
        "Component": "Component",
        "Configuration Item": "Configuration Item",
        "Contact": "Contact",
        "Document": "Document",
        "Epic": "Epic",
        "Issue": "Issue",
        "Platform": "Platform",
        "Release": "Release",
        "Requirement": "Requirement",
        "Risk": "Risk",
        "RPE Report": "RPE Report",
        "Task": "Task",
        "Team": "Team",
        "Test Case": "Testcase",
        "Test Configuration": "Testconf",
        "Test Run": "Testrun",
        "Test Set": "Testset",
        "Time Recording": "Work Log",
        "User Story": "User Story",
    }
    api_name = names_ui2api.get(ui_name)
    if api_name is None:
        raise ValueError(f"Unknown tracker type UI name: '{ui_name}'")
    type_id = type_map.get(api_name)
    if type_id is None:
        raise ValueError(f"Tracker type '{api_name}' not found in API type map")
    return type_id


def main():

    user = codebeamer.get_user(codebeamer_username)

    project_id = int(os.getenv('CODEBEAMER_PROJECT_ID', '25'))

    projects = codebeamer.get_projects()
    projects_map = {project.id: project for project in projects}

    if project_id not in projects_map:
        known_ids = ', '.join(str(pid) for pid in sorted(projects_map.keys())[:20])
        raise ValueError(f'Project with id={project_id} not found in fetched projects. Known IDs (first 20): {known_ids}')

    project = projects_map[project_id]

    print(project.id, project.name)

    project = codebeamer.get_project(project_id)

    trackers_list = project.get_trackers()
    if trackers_list:
        print(f'Trackers count: {len(trackers_list)}')
        for tracker in trackers_list:

            print(f'- {tracker.id}[{tracker.type_id}]: {tracker.name}')

            items = tracker.get_items()

            if items and isinstance(items, list):
                if len(items) > 0:
                    for item in items:
                        print(f"  [-] {item.id}: {item.name}")
                else:
                    print("  [x]")


    tracker_type_name = "Requirement"
    tracker_type_id = tracker_type_id_by_ui_name(tracker_type_name)

    if tracker_type_id < 0:
        raise ValueError(f"Tracker type '{tracker_type_name}' not found.")


    new_tracker = project.create_tracker(
        name=f"REQ-{tracker_type_id}: " + get_current_time(),
        description=f"A new tracker for {tracker_type_name}",
        issue_type_id=tracker_type_id,
        user=user,
        key_name="AUTO-GENERATED",
    )

    if new_tracker:
        print(f'** Created tracker.id={new_tracker.id}: \"{new_tracker.name}\"')

        assert new_tracker.type_id == tracker_type_id, f"Expected created tracker.type_id={tracker_type_id}, but got {new_tracker.type_id}"
        assert new_tracker.type_name == tracker_type_name, f"Expected created tracker.type_name={tracker_type_name}, but got {new_tracker.type_name}"

        item_types = codebeamer.get_item_typemap()
        for type_name in item_types.keys():

            new_item = new_tracker.create_tracker_item(
                name=f"ITEM-" + type_name,
                type_name=type_name,
                description=f"A new item for tracker.id={new_tracker.id}, name={new_tracker.name}",
                )

            t_name = new_item.get_typedef().get('name') if new_item.get_typedef() else 'Unset'
            assert t_name == type_name, f"Expected created item to have type '{type_name}', but got '{t_name}'"

            t_id = new_item.categories[0].get('id', -1) if new_item.categories else -1
            assert t_id == item_types.get(type_name, -1), \
                f"Expected created item to have type id '{item_types.get(type_name, -1)}', but got '{t_id}'"


        deleted = None
        deleted = new_tracker.delete()

        if deleted is not None:
            assert new_tracker.id == -1, f"Expected deleted tracker to have id=-1, but got {new_tracker.id}"
            print(f'Deleted tracker.id={new_tracker.id}: \"{new_tracker.name}\"')


if __name__ == "__main__":
    main()
