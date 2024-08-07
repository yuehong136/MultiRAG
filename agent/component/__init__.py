import importlib


def component_class(class_name):
    m = importlib.import_module("agent.component")
    c = getattr(m, class_name)
    return c
