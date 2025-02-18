import numpy as np
import os
from .message import success, warning
from .robot import Robot, Link, Part, Joint
from .config import Config
from .shapes import Box, Cylinder, Sphere
from .exporter import Exporter
from .exporter_utils import xml_escape, rotation_matrix_to_rpy
from transforms3d.quaternions import mat2quat


class ExporterMuJoCo(Exporter):
    def __init__(self, config: Config | None = None):
        super().__init__()
        self.config: Config = config

        self.draw_collisions: bool = False
        self.no_dynamics: bool = False
        self.additional_xml: str = ""
        self.meshes: list = []
        self.materials: dict = {}
        self.collision_shapes_only: bool = False

        if config is not None:
            self.no_dynamics = config.no_dynamics
            self.collision_shapes_only = config.collision_shapes_only
            self.draw_collisions: bool = config.get("drawCollisions", False)
            additional_xml_file = config.get("additionalXML", "")
            if additional_xml_file:
                with open(
                    config.output_directory + "/" + additional_xml_file, "r"
                ) as file:
                    self.additional_xml = file.read()

    def append(self, line: str):
        self.xml += line

    def build(self, robot: Robot):
        self.xml = ""
        self.append('<?xml version="1.0" ?>')
        self.append("<!-- Generated using onshape-to-robot -->")
        if self.config:
            self.append(f"<!-- OnShape {self.config.printable_version()} -->")
        self.append(f'<mujoco model="{robot.name}">')
        self.append(f'<compiler angle="radian" meshdir="." />')
        self.append(f'<option noslip_iterations="1"></option>')

        if self.additional_xml:
            self.append(self.additional_xml)

        # Boilerplate
        self.append("<default>")
        self.append('<joint frictionloss="0.1" armature="0.005"/>')
        self.append('<position kp="50" kv="5"/>')
        self.append('<default class="visual">')
        self.append('<geom type="mesh" contype="0" conaffinity="0" group="2"/>')
        self.append("</default>")
        self.append('<default class="collision">')
        self.append('<geom group="3"/>')
        self.append("</default>")
        self.append("</default>")

        self.append("<worldbody>")

        self.add_link(robot, robot.get_base_link())

        self.append("</worldbody>")

        self.append("<asset>")
        for mesh_file in set(self.meshes):
            self.append(f'<mesh file="{mesh_file}" />')
        for material_name, color in self.materials.items():
            color_str = "%.20g %.20g %.20g 1" % tuple(color)
            self.append(f'<material name="{material_name}" rgba="{color_str}" />')
        self.append("</asset>")

        self.add_actuators(robot)

        self.append("</mujoco>")

        return self.xml

    def add_actuators(self, robot: Robot):
        self.append("<actuator>")

        for joint in robot.joints:
            if joint.properties.get("actuated", True):
                type = joint.properties.get("type", "position")
                actuator: str = f'<{type} name="{joint.name}" joint="{joint.name}" '

                for key in "class", "kp", "kd", "ki":
                    if key in joint.properties:
                        actuator += f'{key}="{joint.properties[key]}" '

                if "effort" in joint.properties:
                    actuator += f'forcerange="-{joint.properties["effort"]} {joint.properties["effort"]}" '

                if joint.limits is not None and type == "position":
                    actuator += f'ctrlrange="{joint.limits[0]} {joint.limits[1]}" '

                actuator += "/>"
                self.append(actuator)

        self.append("</actuator>")

    def add_inertial(self, mass: float, com: np.ndarray, inertia: np.ndarray):
        # Populating body inertial properties
        # https://mujoco.readthedocs.io/en/stable/XMLreference.html#body-inertial
        inertial: str = "<inertial "
        inertial += 'pos="%.20g %.20g %.20g" ' % tuple(com)
        inertial += 'mass="%.20g" ' % mass
        inertial += 'fullinertia="%.20g %.20g %.20g %.20g %.20g %.20g" ' % (
            inertia[0, 0],
            inertia[1, 1],
            inertia[2, 2],
            inertia[0, 1],
            inertia[0, 2],
            inertia[1, 2],
        )
        inertial += " />"
        self.append(inertial)

    def add_mesh(self, part: Part, class_: str, T_world_link: np.ndarray):
        """
        Add a mesh node (e.g. STL) to the URDF file
        """
        # Retrieving mesh file and material name
        mesh_file = os.path.basename(part.mesh_file)
        mesh_file_no_ext = ".".join(mesh_file.split(".")[:-1])
        material_name = mesh_file_no_ext + "_material"

        # Relative frame
        T_link_part = np.linalg.inv(T_world_link) @ part.T_world_part

        # Adding the geom node
        self.append(f"<!-- Mesh {part.name} -->")
        geom = f'<geom type="mesh" class="{class_}" '
        geom += self.pos_quat(T_link_part) + " "
        geom += f'mesh="{xml_escape(mesh_file_no_ext)}" '
        geom += f'material="{xml_escape(material_name)}" '
        geom += " />"

        # Adding the mesh and material to appear in the assets section
        self.meshes.append(mesh_file)
        self.materials[material_name] = part.color

        self.append(geom)

    def add_shapes(self, part: Part, class_: str, T_world_link: np.ndarray):
        """
        Add pure shape geometry.
        """
        for shape in part.shapes:
            geom = f'<geom class="{class_}" '

            T_link_shape = (
                np.linalg.inv(T_world_link) @ part.T_world_part @ shape.T_part_shape
            )
            geom += self.pos_quat(T_link_shape) + " "

            if isinstance(shape, Box):
                geom += 'type="box" size="%.20g %.20g %.20g" ' % tuple(shape.size / 2)
            elif isinstance(shape, Cylinder):
                geom += 'type="cylinder" size="%.20g %.20g" ' % (
                    shape.radius,
                    shape.length / 2,
                )
            elif isinstance(shape, Sphere):
                geom += 'type="sphere" size="%.20g" ' % shape.radius

            if class_ == "visual":
                material_name = f"{part.name}_material"
                self.materials[material_name] = part.color
                geom += f'material="{xml_escape(material_name)}" '

            geom += " />"
            self.append(geom)

    def add_geometries(
        self, part: Part, T_world_link: np.ndarray, class_: str, what: str
    ):
        """
        Add geometry nodes. "class_" is the class that will be used, "what" is the logic used to produce it.
        Both can be "visual" or "collision"
        """
        if what == "collision" and part.shapes is not None:
            self.add_shapes(part, class_, T_world_link)
        elif part.mesh_file and (what == "visual" or not self.collision_shapes_only):
            self.add_mesh(part, class_, T_world_link)

    def add_joint(self, joint: Joint):
        self.append(f"<!-- Joint from {joint.parent.name} to {joint.child.name} -->")

        joint_xml: str = "<joint "
        joint_xml += f'name="{joint.name}" '
        if joint.joint_type == Joint.REVOLUTE:
            joint_xml += 'type="hinge" '
        elif joint.joint_type == Joint.PRISMATIC:
            joint_xml += 'type="slide" '
        elif joint.joint_type == Joint.FIXED:
            print(warning("Joint type is not supported in MuJoCo: fixed"))
            joint_xml += 'type="free" '

        for key in (
            "class",
            "friction",
            "frictionloss",
            "armature",
            "damping",
            "stiffness",
        ):
            if key in joint.properties:
                if key == "friction":
                    key = "frictionloss"
                joint_xml += f'{key}="{joint.properties[key]}" '

        joint_xml += " />"
        self.append(joint_xml)

    def add_frame(
        self,
        link: Link,
        frame: str,
        T_world_link: np.ndarray,
        T_world_frame: np.ndarray,
    ):
        self.append(f"<!-- Frame {frame} (dummy link + fixed joint) -->")
        T_link_frame = np.linalg.inv(T_world_link) @ T_world_frame

        site: str = f'<site name="{frame}" '
        site += self.pos_quat(T_link_frame) + " "
        site += " />"
        self.append(site)

    def add_link(
        self,
        robot: Robot,
        link: Link,
        parent_joint: Joint | None = None,
        T_world_parent: np.ndarray = np.eye(4),
    ):
        """
        Adds a link recursively to the URDF file
        """
        if parent_joint is None:
            T_world_link = np.eye(4)
        else:
            T_world_link = parent_joint.T_world_joint

        self.append(f"<!-- Link {link.name} -->")
        T_parent_link = np.linalg.inv(T_world_parent) @ T_world_link
        self.append(f'<body name="{link.name}" {self.pos_quat(T_parent_link)} >')

        if parent_joint is None:
            self.append('<freejoint name="root" />')
        else:
            self.add_joint(parent_joint)

        # Adding inertial properties
        mass, com, inertia = link.get_dynamics(T_world_link)
        self.add_inertial(mass, com, inertia)

        # Adding geometry objects
        for part in link.parts:
            self.append(f"<!-- Part {part.name} -->")
            self.add_geometries(
                part,
                T_world_link,
                "visual",
                "collision" if self.draw_collisions else "visual",
            )
            self.add_geometries(part, T_world_link, "collision", "collision")

        # Adding frames attached to current link
        for frame, T_world_frame in link.frames.items():
            self.add_frame(link, frame, T_world_link, T_world_frame)

        # Adding joints and children links
        for joint in robot.get_link_joints(link):
            self.add_link(robot, joint.child, joint, T_world_link)

        self.append("</body>")

    def pos_quat(self, matrix: np.ndarray) -> str:
        """
        Turn a transformation matrix into 'pos="..." quat="..."' attributes
        """
        pos = matrix[:3, 3]
        quat = mat2quat(matrix[:3, :3])
        xml = 'pos="%.20g %.20g %.20g" quat="%.20g %.20g %.20g %.20g"' % (*pos, *quat)

        return xml

    def write_xml(self, robot: Robot, filename: str) -> str:
        scene_xml: str = (
            os.path.dirname(os.path.realpath(__file__)) + "/assets/scene.xml"
        )
        scene_xml = open(scene_xml, "r").read()
        super().write_xml(robot, filename)

        dirname = os.path.dirname(filename)
        with open(dirname + "/scene.xml", "w") as file:
            file.write(scene_xml)
            print(success(f"* Writing {dirname}/scene.xml"))
