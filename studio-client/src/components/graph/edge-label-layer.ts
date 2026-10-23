import type Sigma from "sigma";
import { GLStateGuard } from "sigma/rendering";

import { drawEdgeLabels } from "./edge-labels";

// The upright edge labels as a sigma layer program. Registered at the "nodes" depth, it draws
// after every edge (sigma's "edges" and "topEdges" depths come first) and before every node,
// so edge lines never cross a label and nodes stay on top, as on sigma 3. Each frame draws the
// labels on an offscreen 2D canvas and lays that canvas over the stage as one texture.

type LayerProgramFactory = Parameters<Sigma["addCustomLayerProgram"]>[2];

const VERTEX_SHADER = `#version 300 es
out vec2 v_uv;
void main() {
  // One triangle that covers the viewport, built from the vertex index alone.
  vec2 corner = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  // The canvas's first row is its top, the viewport's bottom is y = -1.
  v_uv = vec2(corner.x, 1.0 - corner.y);
  gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);
}
`;

const FRAGMENT_SHADER = `#version 300 es
precision mediump float;
in vec2 v_uv;
uniform sampler2D u_labels;
out vec4 fragColor;
void main() {
  fragColor = texture(u_labels, v_uv);
}
`;

/**
 * Builds the layer program that draws the upright edge labels inside sigma's render.
 *
 * Args:
 *   sigma: The renderer the program is added to.
 *   focused: Reads the hovered or selected node, or null, when a frame is drawn.
 *
 * Returns:
 *   The factory to pass to `sigma.addCustomLayerProgram`.
 */
export function edgeLabelLayer(sigma: Sigma, focused: () => string | null): LayerProgramFactory {
  return (gl) => {
    const context = document.createElement("canvas").getContext("2d");
    if (context === null) throw new Error("edge label layer: no 2D canvas context");
    const program = linkProgram(gl);
    const sampler = gl.getUniformLocation(program, "u_labels");
    const texture = gl.createTexture();
    const vertexArray = gl.createVertexArray();
    const guard = new GLStateGuard(gl);
    return {
      preRender: (params) => {
        drawEdgeLabels(sigma, context, focused(), params);
      },
      render: () => {
        guard.save();
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.useProgram(program);
        gl.bindVertexArray(vertexArray);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, texture);
        // Premultiplied, to match sigma's blending (ONE, ONE_MINUS_SRC_ALPHA).
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, context.canvas);
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.uniform1i(sampler, 0);
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        guard.restore();
      },
      kill: () => {
        gl.deleteVertexArray(vertexArray);
        gl.deleteTexture(texture);
        gl.deleteProgram(program);
      },
    };
  };
}

function linkProgram(gl: WebGL2RenderingContext): WebGLProgram {
  const program = gl.createProgram();
  for (const [type, source] of [
    [gl.VERTEX_SHADER, VERTEX_SHADER],
    [gl.FRAGMENT_SHADER, FRAGMENT_SHADER],
  ] as const) {
    const shader = gl.createShader(type);
    if (shader === null) throw new Error("edge label layer: cannot create a shader");
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    gl.attachShader(program, shader);
    gl.deleteShader(shader);
  }
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(`edge label layer: ${gl.getProgramInfoLog(program)}`);
  }
  return program;
}
