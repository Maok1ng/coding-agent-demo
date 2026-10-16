import ast
import json


class TodoManager:
    def __init__(self):
        self.items:list[dict] = [] #items 是一个列表 里面每个元素是一个字典 列表和字典都可以修改

    def update(self, todos:list|str) -> str:
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError) as e:
                    raise ValueError("todos must be a list or JSON array string") from e
        if not isinstance(todos, list):
            return "Error: Todos should be a list of dictionaries."
        if len(todos) > 10:
            return "Error: Too many todos. Limit is 10."
        validated = []
        in_progress_count = 0
        for index,todo in enumerate(todos):
            if not isinstance(todo, dict):
                return f"Error: Todo at index {index} is not a dictionary."
            content = str(todo.get("content", "")).strip()
            status = str(todo.get("status", "pending")).strip().lower()
            if not content:
                return f"Error: Todo at index {index} has empty content."
            if status not in ["pending", "in_progress", "completed"]:
                return f"Error: Todo at index {index} has invalid status '{status}'."
            if status == "in_progress":
                in_progress_count += 1
            validated.append({
                "content": content,
                "status": status,
            })
        if in_progress_count > 1:
            return "Error: Only one todo can be 'in progress' at a time."
        self.items = validated
        return self.render()
        

    def render(self):
        if not self.items:
            return "No todos available."
        lines = []
        for todo in self.items:
            marker = { 
                "pending": "[ ]",
                "in_progress": "[~]",
                "completed": "[x]"
            }[todo["status"]]
            lines.append(f"{marker} {todo['content']}")
        return "\n".join(lines)
   

To_do = TodoManager()
def run_todo_write(todos: list | str) -> str:
    try:
        output = To_do.update(todos)
    except ValueError as e:
        return f"Error: {e}"
    print(output)
    return output
