/*
 * architecture-model — источник истины архитектуры (Architecture as Code).
 *
 * Элементы и связи ГЕНЕРИРУЮТСЯ из model/**/*.yaml командой `docsync generate`
 * в model/generated/*.dsl (и подставляются inline при рендере через Kroki).
 * Views ниже ведёт архитектор вручную: это то, что попадает в Confluence как C4-диаграммы.
 * Ключ view ("payments-containers") указывается в docs-registry.yaml (template: c4-view).
 */
workspace "Company Architecture" "Модель архитектуры, поддерживаемая DocSync" {

    !identifiers hierarchical

    model {
        customer = person "Покупатель" "Оформляет и оплачивает заказы"
        !include model/generated/elements.dsl
        !include model/generated/relationships.dsl
        customer -> payments.orders_service "Оформляет заказ" "HTTPS"
    }

    views {
        systemLandscape "landscape" "Ландшафт систем" {
            include *
            autoLayout lr
        }

        systemContext payments "payments-context" "Payments — контекст" {
            include *
            autoLayout lr
        }

        container payments "payments-containers" "Payments — контейнеры" {
            include *
            autoLayout lr
        }

        styles {
            element "Person" {
                shape Person
                background #08427b
                color #ffffff
            }
            element "Software System" {
                background #1168bd
                color #ffffff
            }
            element "Container" {
                background #438dd5
                color #ffffff
            }
            element "Database" {
                shape Cylinder
            }
            element "Queue" {
                shape Pipe
                background #f0ad4e
            }
            element "External" {
                background #999999
                color #ffffff
            }
            relationship "async" {
                style dashed
            }
            relationship "deprecated" {
                colour #cc0000
            }
        }
    }
}
