"""Use the framework's effective routes, including lazy router inclusion."""


def effective_routes(application):
    for route in application.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            yield from contexts()
        else:
            yield route
