# MultiRAG Admin Service & CLI

### Introduction

Admin Service is a dedicated management component designed to monitor, maintain, and administrate the MultiRAG system. It provides comprehensive tools for ensuring system stability, performing operational tasks, and managing users and permissions efficiently.

The service offers real-time monitoring of critical components, including the MultiRAG server, Task Executor processes, and dependent services such as MySQL, Elasticsearch, Redis, and MinIO. It automatically checks their health status, resource usage, and uptime, and performs restarts in case of failures to minimize downtime.

For user and system management, it supports listing, creating, modifying, and deleting users and their associated resources like knowledge bases and Agents.

Built with scalability and reliability in mind, the Admin Service ensures smooth system operation and simplifies maintenance workflows.

It consists of a server-side Service and a command-line client (CLI), both implemented in Python. User commands are parsed using the Lark parsing toolkit.

- **Admin Service**: A backend service that interfaces with the MultiRAG system to execute administrative operations and monitor its status.
- **Admin CLI**: A command-line interface that allows users to connect to the Admin Service and issue commands for system management.



### Starting the Admin Service

#### Launching from source code

1. Before start Admin Service, please make sure MultiRAG system is already started.

2. Launch from source code:

   ```bash
   python admin/server/admin_server.py
   ```
   The service will start and listen for incoming connections from the CLI on the configured port. 

#### Using docker image

1. Before startup, please configure the `docker_compose.yml`  file to enable admin server:

   ```bash
   command:
     - --enable-adminserver
   ```

2. Start the containers, the service will start and listen for incoming connections from the CLI on the configured port.



### Using the Admin CLI

1.  Ensure the Admin Service is running.
2.  Install multirag-cli.
    ```bash
    pip install multirag-cli
    ```
3.  Launch the CLI client:
    ```bash
    multirag-cli -h 0.0.0.0 -p 9130
    ```
	Enter superuser's password to login. Default password is `admin`.



## Supported Commands

Commands are case-insensitive and must be terminated with a semicolon (`;`).

### Service Management Commands

-   `LIST SERVICES;`
    -   Lists all available services within the MultiRAG system.
-   `SHOW SERVICE <id>;`
    -   Shows detailed status information for the service identified by `<id>`.


### User Management Commands

-   `LIST USERS;`
    -   Lists all users known to the system.
-   `SHOW USER '<username>';`
    -   Shows details and permissions for the specified user. The username must be enclosed in single or double quotes.

- `CREATE USER <username> <password>;`
  - Create user by username and password. The username and password must be enclosed in single or double quotes.

-   `DROP USER '<username>';`
    -   Removes the specified user from the system. Use with caution.
-   `ALTER USER PASSWORD '<username>' '<new_password>';`
    -   Changes the password for the specified user.
-   `ALTER USER ACTIVE <username> <on/off>;`
    -   Changes the user to active or inactive.


### Data and Agent Commands

-   `LIST DATASETS OF '<username>';`
    -   Lists the datasets associated with the specified user.
-   `LIST AGENTS OF '<username>';`
    -   Lists the agents associated with the specified user.

### Meta-Commands

Meta-commands are prefixed with a backslash (`\`).

-   `\?` or `\help`
    -   Shows help information for the available commands.
-   `\q` or `\quit`
    -   Exits the CLI application.

## Examples

```commandline
admin> list users;
+-------------------------------+------------------------+-----------+-------------+
| create_date                   | email                  | is_active | nickname    |
+-------------------------------+------------------------+-----------+-------------+
| Fri, 22 Nov 2024 16:03:41 GMT | admin@datav.com   | 1         | admin     |
| Fri, 22 Nov 2024 16:10:55 GMT | dxl@datav.com     | 1         | dxl       |
+-------------------------------+------------------------+-----------+-------------+

admin> list services;
+----+------------+-----------------+-------+-----------------+---------+--------------------------------------------------------------------------------------------------------------------------------------------+
| id | name       | host            | port  | service_type    | status  | extra                                                                                                                                      |
+----+------------+-----------------+-------+-----------------+---------+--------------------------------------------------------------------------------------------------------------------------------------------+
| 0  | multirag_0 | 127.0.0.1       | 8123  | multirag_server | Alive   | {'secret_key': 'multirag', 'admin_require_superuser': False}                                                                               |
| 1  | postgresql | 127.0.0.1       | 5432  | metadata        | Alive   | {'meta_type': 'postgresql', 'username': 'usr_ai', 'password': '123456', 'dbname': 'postgres', 'max_connections': 100, 'stale_timeout': 30} |
| 2  | minio      | 127.0.0.1       | 9000  | file_store      | Alive   | {'store_type': 'minio', 'user': 'minioadmin', 'password': '12345678', 'workflow_bucket': 'multirag-workflow'}                              |
| 3  | milvus     | 127.0.0.1       | 19530 | retrieval       | Alive   | {'retrieval_type': 'milvus', 'username': 'root', 'password': 'Milvus'}                                                                     |
| 4  | infinity   | infinity        | 23817 | retrieval       | Timeout | {'retrieval_type': 'infinity', 'db_name': 'default_db'}                                                                                    |
| 5  | redis      | 127.0.0.1       | 6379  | message_queue   | Alive   | {'mq_type': 'redis', 'database': 1}                                                                                                        |
+----+------------+-----------------+-------+-----------------+---------+--------------------------------------------------------------------------------------------------------------------------------------------+
```