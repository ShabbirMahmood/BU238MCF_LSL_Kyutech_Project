The `genicam` module is the official Python binding of GenICam that is developed and maintained by the GenICam committee.

.. figure:: https://user-images.githubusercontent.com/8652625/44912527-715ca800-ad65-11e8-9a33-9a88d5411340.png
    :alt: GenICam

########################
About the genicam module
########################

The `genicam` module consists of two sub-modules. The one is `genapi` and the other is `gentl`. The `genapi` module gives you a way to control GenICam features of the target device supports. On the other hand, the `gentl` module gives you a way to control functionality that the target GenTL Producer supports.

########################
Using the genicam module
########################

*****************
The genapi module
*****************

In this section, you will learn how to use the `genapi` module.

==========================
Creating a node map object
==========================

First, you create a node map object instantiating the `NodeMap` class:

.. code-block:: python

    from genicam.genapi import NodeMap

    node_map = NodeMap()

And then, load a device description file on the node map object. The `NodeMap` object supports several ways to make it. One is to load the file content as a `str` object:

.. code-block:: python

    file_content = 'blabla'
    node_map.load_xml_from_string(file_content)

Another way is to load the file as an XML file:

.. code-block:: python

    file = an_xml_file
    node_map.load_xml_from_file(file)

The other ways is to load the file as a Zip file:

.. code-block:: python

    file = a_zip_file
    node_map.load_xml_from_zip_file(file)

========================
Changing a feature value
========================

Once you loaded a device description file on a node map object, you would start to control GenICam feature nodes to control the target device. Now assume that we are going to control a GenICam feature node called `Foo`. To get the value of `Foo`, we code as follows:

.. code-block:: python

    a = node_map.Foo.value

On the other hand, if `Foo` is an Integer node then we code as follows to set a value:

.. code-block:: python

    node_map.Foo.value = 42

If `Foo` is a Boolean node, then you code as follows:

.. code-block:: python

    node_map.Foo.value = True

Or if `Foo` is an Enumeration node, then you code as follows; it also works for a case where `Foo` is a String node:

.. code-block:: python

    node_map.Foo.value = 'Bar'

If `Foo` is a Command node, then you can execute the command with the following code:

.. code-block:: python

    node_map.Foo.execute()


****************
The gentl module
****************

In this section, you will learn how to use the `gentl` module.

[TO BE DOCUMENTED]

#####
Links
#####

*********
Harvester
*********

There is a sister project of the GenICam Python binding. It's called Harvester and is a reference implementation of the GenICam Python binding. Harvester gives you an intuitive way for image acquisition to make your life easier.

Harvester is distributed under the Apache version 2 license so you can use it for free; however, note that GenICam applies for another license. The source code can be found at `GitHub <https://github.com/genicam/harvesters>`_.

In addition, Haveseter is also uploaded to PyPi repository so You can install that executing the following pip command:

.. code-block:: shell

    $ pip install harvesters

****
EMVA
****

In `the EMVA website <https://www.emva.org/standards-technology/genicam/genicam-downloads/>`_, you can get useful resources to learn and use GenICam standards and its compliant devices and software:

It would be worth knowing the following keywords: *GenApi*, *GenTL*, *SFNC*, *GenTL SFNC*, *CLProtocol*, *PFNC*, and *GenCP*.
