# Graph-bfs Project

This is a Quarkus port of 503.graph-bfs from
[SeBS: Serverless Benchmark Suite](https://github.com/spcl/serverless-benchmarks).

This application generates an undirected graph of a given number of nodes and
traverses it in breadth-first search (BFS) order.

The input graph is generated by using the Barab&aacute;si-Albert graph generator from
[JGraphT](https://jgrapht.org/).  The implementation of BFS is ported from
[igraph_bfs_simple()](https://github.com/igraph/igraph/blob/master/src/graph/visitors.c#L327)
because JGraphT does not have a BFS method/iterator.


## Preparing Input Data

The input to this application is the number of nodes of a generated graph.
No input file is needed.


## Building and Running the Application

The project can be built as described in [this README](../../README.md).

The application can run as a local HTTP server.
To run the stand-alone Java version:
```shell
java -jar target/quarkus-app/quarkus-run.jar
```
To run the stand-alone native version:
```shell
target/graph-bfs-1.0.0-SNAPSHOT-runner
```


## Sending a Request to the Application

This application receives the following parameters from POST data in JSON format:

|Name         |Value                    |Required?(&starf;)|Default|Default is customizable?|
|:-----------:|:---------------------------------------|:-:|:-----:|:----------------------:|
|size         |Number of nodes of a generated graph    | N |    10 | N |
|debug        |Flag if visited node list is printed out| N | false | N |

&starf; Although both `size` and `debug` can be omitted, an object still must still be sent
as a POST data, e.g. `-d '{}'`.

The `size` parameter can be __an integer__ or __*a data size name*__ as listed below:
|Name  |Number of nodes|
|:----:|:-------------:|
|test  |            10 |
|tiny  |           100 |
|small |         1,000 |
|medium|        10,000 |
|large |       100,000 |


For example:
```shell
curl http://localhost:8080/graph-bfs \
     -X POST \
     -H 'Content-Type: application/json' \
     -d '{"size":"small"}'
```
generates an undirected graph of 1,000 nodes and traverses it in BFS order, but does not return
the list of visited nodes in BFS order because the `debug` parameter is `false`.

Note that returning the resulting BFS list can take much longer than generating and
traversing the graph.  Quarkus runtime serializes the returned node list into JSON, but this is
a time-consuming process.
Therefore, skipping returning the node list is recommended for evaluation of performance.

To send a request to a Knative eventing service,
```shell
curl http://<broker-endpoint>:<port>/ \
     -v \
     -X POST \
     -H 'Ce-Id: 1234' \
     -H 'Ce-Source: curl' \
     -H 'Ce-Specversion: 1.0' \
     -H 'Ce-Type: graph-bfs' \
     -H 'Content-Type: application/json' \
     -d '{"size":30, "debug":"true"}'
```
This request creates a graph of 30 nodes, traversed in BFS order, and returns the visited
node list as a JSON string because `debug` parameter is set to `true`.


## Customizing the Default Value of Input Parameters

This application takes all input parameters from the POST data, and there are no parameters
that can be customized via environment variables or `application.properties`.
