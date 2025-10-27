- prioritize simplicity and readability over checking every single possible thing
- strive to have less code and solutions that achieve the goal with the less code
- actively seek and delete unused/dead code
- do not account for backward compatibility and do not create additional code to support backward compatibility
- always use uv run python instead of just python
- at the end of every task commit all the changes, use spec and task name as commit title, save task summary as commit message

Testing
- only use `bash run-tests.sh` to run tests
- always mock external dependencies such as aws services, slack bolt
- in addition to unit tests always run `git add . && pre-commit run -a`
- when finished working on the task re-run all the tests to make sure they are still in tact