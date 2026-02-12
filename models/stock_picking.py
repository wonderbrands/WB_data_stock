# -*- coding: utf-8 -*-
import logging
import re
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class Picking(models.Model):
    _inherit = 'stock.picking'

    data_fecha_cita_almc = fields.Datetime(string='Fecha cita en almacén')
    
    # "Es IN?"
    data_invisible_field = fields.Boolean(
        string='Es IN?', 
        compute='_compute_is_in_type',
        help='Campo técnico para saber si es recepción'
    )
    
    data_restocked_field = fields.Boolean(string="Is restocked?", default=False)
    
    data_show_full_button = fields.Boolean(
        string="Show full button",
        compute='_compute_show_full_button',
    )

    @api.depends('state', 'data_restocked_field')
    def _compute_show_full_button(self):
        for picking in self:
            # Mostrar solo si es para full y está en estados válidos
            picking.data_show_full_button = (
                picking.data_restocked_field and 
                picking.state in ['draft', 'waiting', 'confirmed', 'assigned']
            )

    @api.depends('picking_type_id')
    def _compute_is_in_type(self):
        # Compute the value of the invisible field based on the picking type.
        # Verificar el 'code' (incoming) que un ID fijo (1)
        for picking in self:
            picking.data_invisible_field = (picking.picking_type_id.code == 'incoming')

    def reservation_from_superior_lvs(self):
        self.ensure_one()
        
        # Definir patrón de ubicaciones
        location_pattern = re.compile(r"(AG/Stock/TL/N|AG/Stock/N)([0-9]|[1-9][0-9]|100)\b")
        
        # Buscar ubicaciones que coincidan
        # Optimizamos la búsqueda trayendo solo IDs primero
        domain = [
            ('usage', '=', 'internal'),
            '|', ('complete_name', 'ilike', 'AG/Stock/TL/N'), ('complete_name', 'ilike', 'AG/Stock/N')
        ]
        candidate_locations = self.env["stock.location"].search(domain)
        
        # Filtramos con regex
        valid_location_ids = [
            loc.id for loc in candidate_locations 
            if location_pattern.match(loc.complete_name)
        ]

        if not valid_location_ids:
            raise UserError("No se encontraron ubicaciones válidas con el patrón AG/Stock/...")

        insufficient_stock_products = []

        # Procesar movimientos
        # Iteramos sobre move_ids (stock.move) en lugar de líneas sin paquete para ser más robustos
        for move in self.move_ids.filtered(lambda m: m.state not in ['done', 'cancel']):
            
            # Si ya tiene algo reservado, calculamos cuánto falta
            qty_needed = move.product_uom_qty - move.quantity # Odoo 18 usa 'quantity' para lo reservado en stock.move
            
            if qty_needed <= 0:
                continue

            _logger.info(f"Procesando {move.product_id.name}. Necesario: {qty_needed}")

            # Buscar quants disponibles en las ubicaciones filtradas
            # Usamos _gather para obtener quants respetando estrategias si fuera necesario, 
            # pero aquí lo haremos directo para cumplir tu requerimiento de ubicaciones específicas.
            quants = self.env['stock.quant'].search([
                ('product_id', '=', move.product_id.id),
                ('location_id', 'in', valid_location_ids),
                ('quantity', '>', 0)
            ], order='location_id desc') # Tu ordenamiento original

            qty_reserved_for_move = 0
            
            for quant in quants:
                available_in_quant = quant.quantity - quant.reserved_quantity
                if available_in_quant <= 0:
                    continue
                
                to_reserve = min(available_in_quant, qty_needed)
                
                # --- FORMA SEGURA DE RESERVAR EN ODOO ---
                # Usamos el método nativo update_reserved_quantity del Quant
                # Esto crea/actualiza los stock.move.line automáticamente y mantiene la integridad.
                try:
                    self.env['stock.quant']._update_reserved_quantity(
                        move.product_id,
                        quant.location_id,
                        to_reserve,
                        location_dest_id=move.location_dest_id,
                        strict=True,
                        move_id=move # Vincular al movimiento
                    )
                    qty_reserved_for_move += to_reserve
                    qty_needed -= to_reserve
                    
                    _logger.info(f"Reservado {to_reserve} de {quant.location_id.complete_name}")
                except Exception as e:
                    _logger.error(f"Error reservando: {e}")

                if qty_needed <= 0:
                    break
            
            if qty_needed > 0:
                insufficient_stock_products.append(move.product_id.name)

        # Recalcular estado del picking
        self.move_ids._recompute_state() 

        # Notificación al usuario
        if insufficient_stock_products:
            msg = f"No se pudo reservar completamente: {', '.join(insufficient_stock_products)}"
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': "Stock Insuficiente", 'message': msg, 'type': 'warning', 'sticky': True}
            }
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': "Éxito", 'message': "Reservación completada exitosamente", 'type': 'success', 'sticky': False}
        }