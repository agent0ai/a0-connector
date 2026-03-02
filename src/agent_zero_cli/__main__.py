def main() -> None:
    from agent_zero_cli.app import AgentZeroCLI
    from agent_zero_cli.config import load_config

    config = load_config()
    app = AgentZeroCLI(config)
    app.run()


if __name__ == "__main__":
    main()
