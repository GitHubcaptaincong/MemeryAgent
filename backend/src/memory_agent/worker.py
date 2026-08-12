from memory_agent.runtime import get_job_runner


def run() -> None:
    get_job_runner().run_forever()


if __name__ == "__main__":
    run()
